# ✈️ Flight Management System

A production-oriented, full-stack aviation management platform designed around **reliable booking workflows, AI-powered policy assistance, and automated operational processes**.

The system combines a **FastAPI backend**, **Next.js frontend**, **Supabase PostgreSQL**, **Pinecone vector search**, **Google Gemini**, and **n8n automation** to provide a complete flight booking and management experience.

## 🚀 Live Demo

| Resource             | Link                                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------------ |
| 🎥 Demo Video        | [Watch Demo](https://drive.google.com/file/d/1k65y4hWraK63De968VzIlVyNuhTvzusL/view?usp=sharing) |
| 🌐 Frontend          | [flight-frontend-eta.vercel.app](https://flight-frontend-eta.vercel.app/)                        |
| ⚡ Backend API        | [Railway Deployment](https://flight-management-system-production-d898.up.railway.app)            |
| 📚 API Documentation | [FastAPI Swagger UI](https://flight-management-system-production-d898.up.railway.app/docs)       |

---

## ✨ Key Features

### 🔒 Resilient State-Machine Booking

The booking system follows explicit lifecycle states and controlled transitions rather than allowing arbitrary booking updates.

It provides:

* Temporary seat holds
* Booking confirmation
* Inventory protection
* Strict state transitions
* Prevention of double booking
* Waitlist management
* Automatic waitlist promotion

This approach keeps booking operations predictable and protects flight inventory during concurrent requests.

---

### 🤖 AI-Powered Policy RAG Assistant

The platform includes a **Retrieval-Augmented Generation (RAG)** assistant for answering aviation policy questions.

The pipeline combines:

**Policy Documents → Embeddings → Pinecone → Relevant Context → Gemini → Answer**

The assistant can retrieve and explain information such as:

* Baggage policies
* Refund rules
* Cancellation policies
* Booking restrictions
* Airline regulations
* Passenger-specific policy questions

The RAG layer allows responses to be grounded in the application's policy knowledge rather than relying solely on the language model's internal knowledge.

---

### ⚙️ Automated n8n Workflows

Operational tasks are automated using **n8n**, reducing the need for manual administrative intervention.

#### Waitlist Auto-Promotion

When seats become available:

1. The workflow checks flight inventory.
2. Identifies eligible waitlisted passengers.
3. Promotes the next passenger in the queue.
4. Updates the booking state.
5. Sends a ticket/confirmation notification through Gmail.

#### Refund Escalation Pipeline

The refund workflow:

1. Finds unresolved refund requests.
2. Detects requests older than seven days.
3. Aggregates stale requests.
4. Generates an administrative alert digest.
5. Sends the notification automatically.

---

# 🏗️ Architecture

```text
                         ┌─────────────────────┐
                         │     Next.js App     │
                         │   Frontend / UI     │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │     FastAPI API     │
                         │  Business Logic     │
                         └──────┬──────┬───────┘
                                │      │
                    ┌───────────┘      └────────────┐
                    ▼                               ▼
          ┌──────────────────┐            ┌──────────────────┐
          │ Supabase /        │            │ Pinecone Vector  │
          │ PostgreSQL        │            │ Database         │
          └──────────────────┘            └────────┬─────────┘
                                                   │
                                                   ▼
                                         ┌──────────────────┐
                                         │ Google Gemini AI │
                                         │ RAG Assistant    │
                                         └──────────────────┘

                         ┌─────────────────────┐
                         │        n8n          │
                         │ Workflow Automation │
                         └──────────┬──────────┘
                                    │
                         ┌──────────┴──────────┐
                         ▼                     ▼
                  Waitlist Automation    Refund Escalation
                         │                     │
                         ▼                     ▼
                       Gmail              Admin Alerts
```

---

# 🛠️ Tech Stack

### Backend

* **Python**
* **FastAPI**
* **Supabase**
* **PostgreSQL**
* **Pinecone**
* **Google Gemini (`gemini-2.0-flash`)**

### Frontend

* **Next.js**
* **App Router**
* **TypeScript**
* **Tailwind CSS**
* **Lucide Icons**

### Automation & Infrastructure

* **n8n**
* **Docker**
* **Railway**
* **Vercel**

---

# 📡 Core API

## Flights & Administration

### Search Flights

```http
GET /api/v1/search
```

Search available flight schedules based on supported search parameters.

### Create Flight

```http
POST /api/v1/admin/flights
```

Creates a new flight and its seat configuration.

> Requires administrator authentication headers.

---

## 🎫 Booking Lifecycle

### Hold Seat

```http
POST /api/v1/bookings/hold
```

Places a temporary hold on available inventory.

### Confirm Booking

```http
POST /api/v1/bookings/confirm
```

Finalizes a held booking and transitions it to the confirmed state.

### Join Waitlist

```http
POST /api/v1/waitlist
```

Adds a passenger to the waitlist when the requested flight/class has no available inventory.

---

## 🧠 AI & RAG

### Ask Policy Question

```http
POST /api/v1/rag/policy-question
```

Retrieves relevant policy information and generates a context-aware response using the RAG pipeline.

---

# 🔄 Booking State Machine

The booking lifecycle is controlled through explicit state transitions.

```text
             ┌──────────────┐
             │    HELD      │
             └──────┬───────┘
                    │
              confirmation
                    │
                    ▼
             ┌──────────────┐
             │  CONFIRMED   │
             └──────┬───────┘
                    │
              cancellation
                    │
                    ▼
             ┌──────────────┐
             │  CANCELLED   │
             └──────────────┘

HELD ──────── expiry ────────► EXPIRED
```

This prevents invalid lifecycle transitions and helps maintain consistent inventory.

---

# 🧠 RAG Pipeline

The AI assistant follows a retrieval-first architecture:

```text
User Question
      │
      ▼
Generate Query Embedding
      │
      ▼
Pinecone Similarity Search
      │
      ▼
Retrieve Relevant Policies
      │
      ▼
Build Context
      │
      ▼
Google Gemini
      │
      ▼
Grounded Policy Response
```

This architecture makes the assistant particularly useful for policy-heavy questions where answers should be based on the application's stored documents.

---

# ⚙️ Local Development

## 1. Clone the Repository

```bash
git clone <your-repository-url>
cd <repository-directory>
```

## 2. Configure Environment Variables

Create your environment configuration with the required credentials for:

* Supabase
* PostgreSQL
* Pinecone
* Google Gemini
* Other application-specific secrets

Example:

```env
SUPABASE_URL=
SUPABASE_KEY=

PINECONE_API_KEY=
PINECONE_INDEX=

GEMINI_API_KEY=
```

> Never commit `.env` files or API keys to Git.

---

## 3. Start the FastAPI Backend

Install the project dependencies and start the development server:

```bash
uvicorn main:app --reload --port 8000
```

The API will be available at:

```text
http://localhost:8000
```

Swagger documentation:

```text
http://localhost:8000/docs
```

---

## 4. Run n8n Locally

Run n8n with persistent Docker storage:

```bash
docker run -d \
  --name n8n \
  -p 5678:5678 \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n
```

Open the n8n interface:

```text
http://localhost:5678
```

The persistent volume ensures that your n8n workflows and configuration survive container restarts.

---

# 📁 Project Capabilities

| Area                | Capability                          |
| ------------------- | ----------------------------------- |
| ✈️ Flights          | Flight search and administration    |
| 🎫 Bookings         | Seat holds and confirmations        |
| 🔒 Inventory        | State-based inventory protection    |
| 🕐 Waitlist         | Queue management and promotion      |
| 🤖 AI               | Context-aware policy assistant      |
| 🔎 RAG              | Pinecone-powered document retrieval |
| ⚙️ Automation       | n8n background workflows            |
| 📧 Notifications    | Automated Gmail notifications       |
| 💰 Refunds          | Automated refund escalation         |
| 🗄️ Database        | Supabase PostgreSQL                 |
| ☁️ Deployment       | Vercel + Railway                    |
| 🐳 Local Automation | Docker + n8n                        |

---

# 🎯 Engineering Highlights

This project focuses on several real-world backend engineering challenges:

* **State-machine driven business logic**
* **Inventory consistency**
* **Concurrent booking protection**
* **RAG-based AI integration**
* **Vector similarity search**
* **Event-driven workflow automation**
* **Automated operational escalation**
* **Persistent workflow infrastructure**
* **Cloud deployment**
* **API-first architecture**

Rather than treating AI and automation as isolated features, the system integrates them directly into the operational workflow of the aviation platform.

---

# 📌 Project Status

**Production-oriented / deployed prototype**

The core backend, frontend, RAG assistant, booking workflow, and n8n automation pipelines are implemented and deployed.

---

## 📄 License

Add your preferred license here, for example:

```text
MIT License
```
