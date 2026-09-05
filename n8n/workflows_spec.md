1. Waitlist Auto-Promotion Job

Trigger: Schedule Trigger node — interval every 2 minutes.

Why 2 minutes: a freed seat (from a cancellation or expired hold) needs to reach the next waitlisted passenger quickly enough that the offer still feels timely, but polling every few seconds just hammers Postgres for an event that's relatively rare (seats don't free up that often outside of cancellation waves). 1–2 min is the usual sweet spot for this class of problem; if cancellations spike around schedule-change events, you can add a second trigger (e.g. a Postgres LISTEN/NOTIFY webhook fired from FastAPI on cancellation) for near-instant promotion, with the poll as a safety net catching anything the notify missed.

Node 1 — Postgres node (Execute Query):

sql
SELECT * FROM promote_next_waitlisted();

Since this is a SECURITY DEFINER-style function using FOR UPDATE SKIP LOCKED internally, concurrent runs (this poll overlapping a manual gate-agent promotion) simply skip rows already locked by another transaction rather than blocking or double-assigning. That's the core of your "row-locking so a gate-agent action and an n8n promotion run can't assign the same freed seat twice" requirement — enforce it inside the function, not in n8n logic.

Node 2 — IF node: branch on whether the function returned any rows (i.e., a promotion actually happened).

Node 3 (on promotion) — Postgres node: fetch the promoted passenger + flight details for notification:

sql
SELECT b.id AS booking_id, b.passenger_email, b.passenger_name,
       f.flight_number, f.origin, f.destination, f.departure_datetime,
       w.claim_deadline
FROM bookings b
JOIN flights f ON f.id = b.flight_id
JOIN waitlist w ON w.booking_id = b.id
WHERE b.id = '{{ $json.promoted_booking_id }}';

Node 4 — Gmail node: send the "seat available — claim within X hours" notification using the fields above.

Node 5 — Postgres node (idempotency/log): insert a row into an n8n_execution_log (or reuse admin_audit_log with a system = 'n8n' tag) so double-runs are traceable and reconciliation jobs can spot anomalies.

2. Check-in Reminder Email

Trigger: Schedule Trigger — every 1 hour (hourly cadence gives you a workable ±1hr window around "exactly 24 hours out" without needing per-minute precision).

Node 1 — Postgres node:

sql
SELECT b.id AS booking_id, b.passenger_email, b.passenger_name,
       f.flight_number, f.origin, f.destination,
       f.departure_datetime, f.origin_timezone, f.destination_timezone
FROM bookings b
JOIN flights f ON f.id = b.flight_id
WHERE f.departure_datetime BETWEEN NOW() + INTERVAL '23 hours 30 minutes'
                                AND NOW() + INTERVAL '24 hours 30 minutes'
  AND b.status = 'confirmed'
  AND f.status != 'cancelled'
  AND NOT EXISTS (
      SELECT 1 FROM notification_log nl
      WHERE nl.booking_id = b.id AND nl.type = 'checkin_reminder'
  );

The NOT EXISTS clause is the de-duplication guard — without it, an hourly poll with a 1-hour window will pick up the same booking on adjacent runs. It also covers your "suppress reminders for already-cancelled flights" edge case via f.status != 'cancelled'.

Node 2 — Function/Set node: convert departure_datetime (stored UTC) into origin_timezone for display in the email body, so the reminder shows local departure time, not server time.

Node 3 — Gmail node: one send per row (use n8n's "Split In Batches" if the query can return many rows), templated with flight number, local departure time, and a check-in link.

Node 4 — Postgres node: insert into notification_log (booking_id, type, sent_at) immediately after each successful send — this is what makes Node 1's NOT EXISTS guard work on the next run.

3. Unresolved Refund Escalation

Trigger: Schedule Trigger — once daily (this is a slow-moving compliance/ops concern, not a real-time one; daily is enough resolution for a 7-day threshold).

Node 1 — Postgres node:

sql
SELECT r.id AS refund_id, b.id AS booking_id, b.passenger_email,
       f.flight_number, r.status, r.requested_at,
       NOW() - r.requested_at AS age
FROM refunds r
JOIN bookings b ON b.id = r.booking_id
JOIN flights f ON f.id = b.flight_id
WHERE r.status NOT IN ('completed', 'denied')
  AND r.requested_at < NOW() - INTERVAL '7 days'
  AND NOT EXISTS (
      SELECT 1 FROM notification_log nl
      WHERE nl.refund_id = r.id AND nl.type = 'refund_escalation'
  );

Node 2 — Gmail node: send to an ops/human-review distribution list (not the passenger) with the refund_id, booking_id, and age — this is your "human sign-off" queue, matching the Approval & Autonomy Boundaries requirement that unresolved refunds beyond policy need a human, not an auto-resolution.

Node 3 — Postgres node: insert into notification_log so it doesn't re-escalate the same refund daily; optionally add an escalation_count column so a refund unresolved for 14, 21 days re-escalates with increasing urgency rather than going silent after the first flag.

One structural note that applies to all three: since n8n has no shared code contract with FastAPI, every one of these queries should be defensive about state that FastAPI might change mid-poll — e.g. Node 1 in the promotion job trusts the SQL function's internal locking rather than n8n doing a read-then-write itself. Keep that pattern (push the atomicity into a Postgres function, let n8n just call it and react) for any future workflow that touches seat or refund state, rather than having n8n read a row, decide, then write back — that's exactly the race window FOR UPDATE SKIP LOCKED exists to close, and n8n-side logic can't replicate it.