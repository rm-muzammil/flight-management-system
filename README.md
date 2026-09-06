Flight Management System
A production-ready, full-stack aviation platform featuring resilient state-machine booking safeguards, an AI-powered RAG policy assistant, and automated background workflows powered by n8n.

Frontend Application: Live App

Backend API: Railway Service

API Documentation: Swagger UI

Tech Stack
Backend: FastAPI, Python, Supabase (PostgreSQL), Pinecone Vector Search, Google Gemini AI (gemini-2.0-flash)

Frontend: Next.js App Router, TypeScript, Tailwind CSS, Lucide Icons

Workflow Automation: n8n, Railway, Vercel, Docker

Key Features & Architecture
State-Machine Bookings: Enforces strict inventory guards and lifecycle transitions, managing seat holds, automatic confirmations, and waitlist allocations without double-booking errors.

AI Policy RAG Assistant: Integrates Pinecone vector embeddings with Google Gemini to parse airline regulations, baggage rules, and refund guidelines dynamically based on active booking context.

Automated n8n Workflows:

Waitlist Auto-Promotion: Periodically scans flight classes for newly available seats, automatically promotes the next waitlisted passenger, and delivers ticket notices via Gmail.

Refund Escalation Pipeline: Identifies unresolved refund requests older than seven days, aggregates stale records, and triggers automated administrative alert digests.

Core API Endpoints
Flights & Administration:

GET /api/v1/search — Search available flight schedules.

POST /api/v1/admin/flights — Create new flights and seat configurations (requires admin headers).

Booking Lifecycle:

POST /api/v1/bookings/hold — Place a temporary inventory hold.

POST /api/v1/bookings/confirm — Finalize booking status.

POST /api/v1/waitlist — Join flight waitlist queues when capacity is full.

AI & Intelligence:

POST /api/v1/rag/policy-question — Query context-aware policy documents using retrieval-augmented generation.

Getting Started Locally
Clone the repository and configure environment variables for Supabase, Pinecone, and Gemini.

Spin up the FastAPI server locally:

Bash
uvicorn main:app --reload --port 8000
Run n8n locally with persistent volume storage for workflow automation:

Bash
docker run -d --name n8n -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n