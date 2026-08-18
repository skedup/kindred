---
name: install-kindred
description: Install or repair Kindred for an existing supported Mouth host when the operator explicitly asks for Kindred setup.
---

# Install Kindred

Use the canonical Kindred release bootstrap and interactive installer. Do not reproduce their
installation logic.

1. Explain that the machine must be macOS 14+ arm64 or Ubuntu 24.04 x86_64 and must already run a
   Mouth host identity listed by the installed Kindred release. Experimental and unverified identities
   remain clearly labeled; the Python installer performs the real host contract checks.
2. Explain that Heart needs an LLM credential and home resolution needs a map credential. Never ask
   the operator to paste a credential into chat.
3. Run only the operator-approved, versioned bootstrap from the official release. It installs local
   verified bytes without probing the Mouth host, then continues into `kindred install` on a controlling TTY.
4. When the installer requests a credential, pause and ask the operator to continue in a secure
   terminal. Do not relay, inspect, or store the value.
5. After installation, run `kindred doctor --json`. Summarize check ids and statuses without showing
   configuration contents or identifiers.

Never read or modify Persona files, Kindred State, databases, messages, memory, configuration, or
resident data yourself. Do not start services, send messages, or call providers unless the operator
separately approves that action. If the bootstrap or installer fails, report its safe error and rerun
the same canonical path only after the operator resolves the cause.
