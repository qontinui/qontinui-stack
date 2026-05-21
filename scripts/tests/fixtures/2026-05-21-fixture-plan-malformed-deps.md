# Plan: Plan with malformed Depends-On (extra commas + trailing comma)

> **Status: VETTED 2026-05-21.** Tolerate stray commas. Depends-On: 2026-05-20-fixture-shipped-dep, 2026-05-20-fixture-second-shipped-dep , 2026-05-21-fixture-draft-dep ,

## Body

Three real stems, one trailing empty token. Resolver must drop the empty
and parse the three real entries.
