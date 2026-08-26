# Contributing to TechPulse OS

Thank you for your interest in contributing to TechPulse OS!

## Pull Request Guidelines
1. Fork the repository and create a branch from `main`.
2. Ensure all Python code compiles cleanly with zero linting or type errors.
3. Run the automated test suite: `python tests/test_endpoints.py`.
4. Ensure no API keys, private credentials, or internal URLs are committed.
5. Submit your PR with a clear, specification-oriented description of the changes.

## Code Standards
- Python 3.11+
- Asynchronous I/O (`async`/`await` with FastAPI, httpx, edge-tts)
- Strict grounding for AI models with verifiable primary source citations