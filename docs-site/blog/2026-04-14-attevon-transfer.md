---
slug: opentranscribe-joins-attevon
title: "OpenTranscribe is Now Home at Attevon LLC"
authors: [davidamacey]
tags: [announcement, open-source, milestone, community]
---

OpenTranscribe has a new home on GitHub: [github.com/attevon-llc/OpenTranscribe](https://github.com/attevon-llc/OpenTranscribe). The project has transferred from my personal account to [Attevon LLC](https://attevon.com), the company I founded to build practical AI tools that actually work in production. Here's what that means and why I made the move.

<!-- truncate -->

## What Is Attevon?

Attevon LLC is a software company focused on a simple idea: **Practical AI, Built to Work.** Not AI demos. Not research prototypes. Tools that run reliably in real environments — on your hardware, in your infrastructure, with your data.

OpenTranscribe is Attevon's flagship open source project — and it has been from the start. What began as a personal side project grew into a production-grade platform that processes thousands of hours of audio, runs in enterprise environments, and has contributors shipping real features. At that scale, it made sense to give it a proper institutional home rather than keep it tied to a personal GitHub account.

## Nothing Changes for You

If you're using OpenTranscribe today, nothing breaks. GitHub automatically redirects all old URLs — your `git remote`, Docker pull commands, and any bookmarked links to `github.com/davidamacey/OpenTranscribe` continue to work transparently.

Specifically:

- **License**: Still [AGPL-3.0](https://github.com/attevon-llc/OpenTranscribe/blob/master/LICENSE). No changes, no exceptions, no dual-licensing bait-and-switch.
- **Docker Hub**: Images remain at `davidamacey/opentranscribe-*` on Docker Hub. Existing `docker compose pull` commands work unchanged.
- **Docs**: Still at [docs.opentranscribe.app](https://docs.opentranscribe.app)
- **Issues & PRs**: All history transferred. Open issues, pull requests, and discussions are all intact at the new URL.
- **Development**: Same maintainer (me), same process, same values.

## Why Make the Move?

A few reasons came together at once.

**The code is identical. The license is unchanged.** To be completely explicit: not a single line of OpenTranscribe's source code changed as a result of this transfer. It is the same software, the same AGPL-3.0 license, the same self-hosted, privacy-first design. The only thing that moved is which GitHub organization hosts the repository.

**IP protection.** A personal GitHub account offers no formal intellectual property protections. As OpenTranscribe has grown — upstream contributions to PyAnnote and WhisperX, enterprise authentication, neural search — formalizing ownership under a registered LLC provides the legal structure to protect the project's IP, enforce the AGPL license against bad actors, and defend the open source community's rights. The AGPL requires that anyone distributing modifications must publish their source code. That's a commitment we intend to uphold, and a company has the legal standing to do so.

**Sustainability.** A project that processes real audio at scale and has enterprise authentication requirements is not a weekend hobby anymore. Housing it under a company gives it access to proper resources — dedicated infrastructure, legal backing, the ability to bring on contributors as the project grows.

**Clarity.** When organizations evaluate self-hosted software, they reasonably ask: *who maintains this, and will it be around in two years?* "A registered company called Attevon maintains this" is a cleaner, more credible answer than a personal GitHub account. The open source model remains the foundation — the company backing makes the long-term story more solid.

**Separation of concerns.** My personal GitHub is where I experiment. Attevon's org is where production software lives. Keeping them separate is just good hygiene.

## What This Means Going Forward

The project roadmap doesn't change because of the transfer — the work happening right now (live/streaming transcription, RAG-based Q&A over transcript libraries, deeper analytics, mobile companion) was already in motion and continues as planned.

What the Attevon home enables is a clearer path to:

- **Commercial support offerings** for organizations that want SLAs and dedicated assistance — without compromising the open source core that everyone can use freely
- **Sustainable contributor compensation** as the project grows
- **Formal vendor relationships** with cloud providers, hardware partners, and enterprise customers

None of that changes the fundamental deal: OpenTranscribe is free, self-hosted, open source software under AGPL-3.0. Attevon's business model sits alongside that, not on top of it.

## Update Your Bookmarks

The redirect is automatic, but if you want to update your remotes explicitly:

```bash
git remote set-url origin https://github.com/attevon-llc/OpenTranscribe.git
```

And the new canonical links:

- **GitHub**: [github.com/attevon-llc/OpenTranscribe](https://github.com/attevon-llc/OpenTranscribe)
- **Issues**: [github.com/attevon-llc/OpenTranscribe/issues](https://github.com/attevon-llc/OpenTranscribe/issues)
- **Attevon**: [attevon.com](https://attevon.com)

Thank you to everyone who has starred, filed issues, submitted PRs, and shared feedback — that community is exactly why formalizing this made sense. The project is in better shape than it's ever been, and it's just getting started.

— David Macey
Founder, Attevon LLC
