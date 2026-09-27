# Security

QOR's public tree must not contain secrets.

Do not commit:

- `.env` or other local environment files
- API keys
- tokens
- passwords
- private credentials of any provider

The language-model planner reads provider settings from the process environment. Those values stay outside the repository.

Do not commit private development material, local notes, or unpublished working files into a public repository. Anything committed to a public repository is public.

If you believe you have found a vulnerability, report it privately to the maintainers of the repository you are using. Do not include live secrets, tokens, or environment dumps in a public issue or pull request. Describe the impact and the version, and wait for a private channel if one is offered.

This file does not publish a security contact address because none has been selected.
