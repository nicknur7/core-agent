# Brain vault

This is Core's long-term memory: a markdown vault that every session gets distilled into,
plus the Postgres graph built from it. It lives in its own repository, separate from Core
itself, because it is *your* content — Core is the engine, this is everything the engine has
learned about you.

Created by `bin/init-brain.sh` from the template that ships with Core. It starts empty. That
is expected and not a misconfiguration: there is nothing to know about you yet.

## Layout

```
entities/    one page per person, company, or thing that recurs
topics/      one page per subject that spans sessions
tools/       one page per tool or system you work with
projects/    one folder per project — session logs and a rollup
_build/      the pipeline that writes all of the above
```

Pages under `entities/`, `topics/` and `tools/` are *hub pages*. Each is compiled from the
evidence beneath it rather than written by hand, so a hub reflects what actually happened
across many sessions instead of what someone remembered to record once.

## How it fills up

`_build/update-brain.sh` runs at session close, called by Core's Stop hook. It exports new
sessions, rebuilds the hub pages, and commits. It is incremental and idempotent — an empty
diff produces no commit, so running it twice costs nothing.

Set `CORE_BRAIN` to this directory before anything will work. The scripts fail loudly rather
than falling back to a default path: a hardcoded fallback once masked a read/write divergence
for a full day, where Core was reading one vault and writing to another.

## What you configure

`_build/consolidate.py` holds two alias maps, both deliberately near-empty here:

- `ENTITY_ALIASES` — nicknames to canonical names, so one person does not become three nodes.
- `TOPIC_ALIASES` — secondary topic slugs merged into a canonical one.

They ship empty of people and private organisations on purpose. In a working vault those maps
are a list of real names, and the author's own copy was published in this template before that
was caught. Fill them in locally; they are your data, not the template's.

## Back it up

Add a **private** remote. The vault is the one part of Core that cannot be rebuilt from source
— lose it and you lose every session that was ever distilled.

```bash
cd "$CORE_BRAIN" && git remote add origin <your-private-repo-url>
```
