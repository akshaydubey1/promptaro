# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Terms of Use gate. Author: Akshay Dubey

Promptaro won't serve anything until the person using this computer has read
and accepted the Terms of Use. Acceptance is recorded in local settings with
the terms version and date — nothing is transmitted. Bumping TERMS_VERSION
re-prompts everyone.
"""
from __future__ import annotations

import time

TERMS_VERSION = "1.0"

TERMS_TEXT = f"""PROMPTARO — TERMS OF USE AND DISCLAIMER
Version {TERMS_VERSION} — Akshay Dubey

By clicking "I agree" you accept these terms. If you do not agree, do not use Promptaro.

1. A personal tool. Promptaro is a desktop application by Akshay Dubey ("the Author"),
provided free of charge, as is, for lawful personal and professional use. It is a tool
that runs on your computer, not a service; using it forms no service relationship.

2. Third-party services are your responsibility. Promptaro connects to AI providers and
tools (Anthropic, OpenAI, OpenRouter, Google, Hugging Face, Together AI, Cloudflare,
Pollinations, Ollama, subscription tools like Claude Code and Codex, and any endpoints
you add). Each is governed by its own terms and prices. You are solely responsible for:
(a) having the right to use every account, key, subscription and endpoint you connect;
(b) complying with every provider's terms; (c) every request made with your accounts
and keys; and (d) everything you send to any provider.

3. Costs. Some providers charge per request. Promptaro shows cost estimates and spending
controls as a convenience, but it cannot guarantee that any request is free. You are
responsible for all charges on your accounts — including charges from models, custom
endpoints, retries, and image or video generation you choose to run.

4. Your data. Promptaro runs locally: conversations, files and keys stay on this computer
except where you choose to send them to a provider. You are responsible for what you
send — including secrets, personal data, confidential code and third-party content —
and for meeting the privacy, employment and confidentiality obligations that apply to you.

5. AI output is not advice. Model output — including code, media and analysis — may be
wrong, unsafe, infringing or inappropriate, and is never professional, legal, medical
or financial advice. Review everything before relying on it. You are responsible for
code and files created or changed by agents you run, and for how generated output is
used and shared.

6. Ownership. You own your inputs and, to the extent your provider's terms allow, the
outputs generated for you. Promptaro itself is proprietary software of the Author; you
get a personal, non-exclusive license to use it — not to sell it, sublicense it, or
present it as your own.

7. No warranty. TO THE MAXIMUM EXTENT PERMITTED BY LAW, PROMPTARO IS PROVIDED "AS IS"
AND "AS AVAILABLE", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, NON-INFRINGEMENT,
ACCURACY OR AVAILABILITY.

8. Limitation of liability. TO THE MAXIMUM EXTENT PERMITTED BY LAW, THE AUTHOR AND ANY
CONTRIBUTORS ARE NOT LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL OR
PUNITIVE DAMAGES, OR FOR ANY LOSS OF DATA, PROFITS, REVENUE, ACCOUNTS, KEYS, FILES OR
GOODWILL, OR FOR PROVIDER ACTIONS (RATE LIMITS, SUSPENSIONS, PRICE CHANGES, ACCOUNT
CLOSURES), ARISING FROM OR RELATED TO YOUR USE OF PROMPTARO. IN ANY CASE, TOTAL
LIABILITY IS LIMITED TO THE AMOUNT YOU PAID FOR PROMPTARO IN THE PREVIOUS 12 MONTHS,
WHICH MAY BE ZERO.

9. Indemnity. You agree to indemnify and hold harmless the Author from any claim, loss
or expense (including reasonable legal fees) arising from your use of Promptaro, your
accounts and keys, the content you submit, or your breach of these terms or any third
party's terms.

10. Changes. These terms may change with new versions of the app; the app asks again
when they do. Where the law gives you rights that cannot be waived, this agreement
limits liability only to the extent the law allows.

Acceptance is recorded on this computer only — the terms version and the date.
Nothing about you is transmitted anywhere.
"""


def accepted() -> bool:
    """True once this computer's user has accepted the current version."""
    from router import catalog
    return catalog.get_settings().get("terms_version") == TERMS_VERSION


def accept() -> dict:
    from router import catalog
    return catalog.set_settings(terms_version=TERMS_VERSION,
                                terms_accepted_at=time.strftime("%Y-%m-%d %H:%M"))
