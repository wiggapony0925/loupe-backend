"""Social domain services, one module per concern.

profiles    — me, claim/update, avatar, deactivation
engagement  — profile views, visit tracking, likes
graph       — follow/unfollow, requests, follower lists
discovery   — search, suggestions, the composed discover feed
collections — shared vaults: portfolios, sets, items, friend owners

The feed half of the surface:

feed_common — caption parsing, cursors, the privacy predicate, payload assembly
posts       — write/delete/like posts; the following · for you · mine feeds
comments    — threaded comments and their likes
hashtags    — trending chips, a tag's feed, tag search
feed_notify — feed events → the notification inbox
"""
