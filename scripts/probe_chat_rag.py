#!/usr/bin/env python3
"""Live probe of the real chat-RAG HTTP path (issue #72), against a real LLM.

Entry point only: the implementation, and the full ``--help`` text, live in the
``scripts/chat_probe/`` package.
"""

from chat_probe.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
