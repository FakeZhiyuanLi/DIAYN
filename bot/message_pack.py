"""
message_pack.py
~~~~~~~~~~~~~~~
Long replies cut into messages Discord will accept.

A module of its own, so that the finder's pure modules can pack their own
replies without importing the Discord client, which needs discord.py and a
token. No imports at all, so it loads anywhere the suite runs.
"""

#: Under Discord's hard 2,000-character ceiling, with room for a header or a
#: code fence a caller wraps around a chunk.
MAX_CHUNK: int = 1850


def pack(items: list[str], limit: int = MAX_CHUNK, sep: str = "\n") -> list[str]:
    """
    Greedily pack strings into chunks of at most `limit` chars, joined by
    `sep`. An item longer than `limit` is hard-sliced, so no chunk can ever
    exceed the cap — Discord rejects anything over 2000 chars outright.
    The input list is not modified.
    """
    chunks: list[str] = []
    current: list[str] = []
    length = 0

    def flush():
        nonlocal current, length
        if current:
            chunks.append(sep.join(current))
            current, length = [], 0

    for item in items:
        if len(item) > limit:
            flush()
            chunks.extend(item[i:i + limit] for i in range(0, len(item), limit))
            continue
        if length + len(item) + len(sep) > limit and current:
            flush()
        current.append(item)
        length += len(item) + len(sep)
    flush()
    return chunks
