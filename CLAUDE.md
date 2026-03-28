# La Flor Blanca Labor Alert System — Claude Code Rules

## Code Architecture
- Always use OOP: organize code into classes and modules, never procedural scripts with global state
- No single file should exceed 300 lines
- Split code into logical modules from the start (e.g. `config.py`, `square_client.py`, `notifier.py`, `scheduler.py`)
- Each class has one responsibility; no "god classes" that do everything

## Project Context
- This is a labor cost alert system for La Flor Blanca restaurant in LA
- Built on Square API (sales + labor data) and Twilio/SendGrid (SMS + email alerts)
- Deployed on Railway, configured via environment variables
- The goal is to evolve into a full AI business advisor powered by Claude API

## Development Workflow
- One GitHub Issue per feature — open a session, reference the issue, build it, close it
- Always develop on a feature branch, merge to master when done
- Never push broken code to master — master is always the live production version
- Commit messages should be clear and describe the "why" not just the "what"

## GitHub Issues (Roadmap)
- Issue #1: Claude AI Advisor in Alert Emails (Phase 1 — do this next)
- Issue #2: Menu Profitability + Customer Pattern Analysis (Phase 2)
- Issue #3: External Signal Awareness — events, reviews, trends (Phase 3)
- Issue #4: Autonomous Marketing + Catering Outreach Agent (Phase 4)
