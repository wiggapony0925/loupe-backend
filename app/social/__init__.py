"""Social — the community layer (follows, profiles, shared collections).

A deliberately self-contained vertical slice: models, schemas, service,
avatar storage, and router all live in this package so the social surface
can grow without scattering across the codebase. MVP ships the Instagram
core: claimable @usernames, profile pictures, bio + location, private
accounts with follow requests, the follow graph, user search, and
privacy-gated viewing of another collector's vault.

Planned expansions (see README.md in this package):
* trade requests between collectors
* posts (pulls, showcases) + reactions/comments
* direct messages
* blocking + reporting
"""
