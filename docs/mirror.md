# A MusicBrainz mirror of your own

Optional (roadmap phase 7). Groovehound works against musicbrainz.org as it is; a mirror of your
own makes every MusicBrainz request local, so album pages load their track lists in a fraction
of a second, and, once phase 7b lands, search can answer as you type. This page is written from a
real build (November 2026) on a TrueNAS server, in an Incus LXC. The mirror is yours to set up
and keep running: Groovehound only talks to its web service (`/ws/2`), like any MusicBrainz
server, never to its database.

The mirror itself is [musicbrainz-docker](https://github.com/metabrainz/musicbrainz-docker),
MetaBrainz's own. Its README is the reference; this page adds what it doesn't say and what went
wrong. Commands below are as its README gave them in November 2026 (MB Solr 4.1, Postgres 18);
check there for changes.

## What it costs

- **Search is most of it.** The database and web service alone answer lookups and browses
  (all Groovehound needs today) but not `?query=` searches; those need the search containers
  (Solr, and the indexer that keeps it current). The README recommends 16 threads, 16 GB of RAM
  and 350 GB of disk with search; 2 threads, 4 GB and 100 GB without.
- **What ran here:** an LXC with a 12 GiB memory limit and a DRAM-less NVMe drive, with search,
  replication and live indexing. Fast enough (see "Speed" below), but the slow drive caused the
  index loading trouble below.
- **Disk:** the full data dumps, the database, and about 60 GB of pre-built search index archives
  to download (removable once loaded).

## Setting it up

1. Clone it and build:

   ```bash
   git clone https://github.com/metabrainz/musicbrainz-docker.git
   cd musicbrainz-docker
   docker compose build
   ```

2. Create the database from the latest dumps (a long download and import):

   ```bash
   docker compose run --rm musicbrainz createdb.sh -fetch
   ```

3. **Memory.** `db` and `search` get about 2 GB each by default (Solr's Java heap is 2 GB).
   Give them more with a local compose file, `local/compose/memory-settings.yml`:

   ```yaml
   services:
     db:
       command: postgres -c "shared_buffers=4GB"
     search:
       environment:
         - SOLR_HEAP=4g
   ```

   ```bash
   admin/configure add local/compose/memory-settings.yml
   ```

4. **Search indexes: load the pre-built ones**, rather than reindexing (which the README puts at
   4½ hours with 16 threads and 16 GB):

   ```bash
   docker compose up -d musicbrainz search
   docker compose exec search fetch-backup-archives
   docker compose exec search load-backup-archives
   ```

   Then **check every collection's document count** before going on (see "Index loading" below:
   it can report success and leave a collection empty). Once they all look right:

   ```bash
   docker compose exec search remove-backup-archives
   ```

5. **Replication**, to keep the database current: it needs a MetaBrainz access token (from your
   MetaBrainz account).

   ```bash
   admin/set-replication-token
   admin/configure add replication-token
   docker compose up -d
   ```

6. **Live indexing**, to keep search current as replication applies changes (search indexes
   aren't replicated). Marked experimental upstream ("not stable yet"); it has worked here.

   ```bash
   admin/configure rm replication-cron
   docker compose up -d
   admin/setup-sir install
   admin/configure add live-indexing-search
   docker compose up -d
   admin/configure add replication-cron
   docker compose up -d
   ```

   The first run had a backlog of about 1.37 million rows to index, cleared at roughly 41 rows a
   second (about nine hours). `replication-cron` runs replication daily (3 am UTC by default).

The web service listens on port 5000 (`MUSICBRAINZ_WEB_SERVER_PORT` to change it). Groovehound
needs to reach that port only. **Don't publish the others** (`publishing-all-ports` also
exposes Postgres, Valkey and Solr, which the README warns aren't safe to expose); keep the mirror
on your own network, not the internet.

## In an LXC on TrueNAS

An unprivileged Incus LXC works, with no VM needed, after two fixes:

- **Docker's `runc` failed on a sysctl** inside the container. Fixed by setting
  `raw.lxc: lxc.apparmor.profile=unconfined` on the LXC (on TrueNAS) *and* purging `apparmor`
  inside it. Check both after a TrueNAS upgrade.
- **Storage:** the mirror's data sits on a ZFS dataset of its own, mounted into the LXC, with a
  16K record size (for the database's small random reads).

## Index loading: check `numDocs`

On the slow drive, `load-backup-archives` silently left the big collections (`recording`
especially) empty, while reporting success. Three things combine:

- the restore has a timeout of roughly three minutes, which a big collection on a slow drive
  exceeds;
- its status check treats a `500` from Solr as success;
- it then deletes the archive files Solr is still reading.

So after loading, ask Solr for each collection's document count (a `*:*` query with `rows=0`,
reading `numFound`, or the core's `numDocs`) and compare with what MusicBrainz holds (millions of
recordings and releases; an empty or tiny one is the giveaway). To redo a single collection,
restore it with Solr's Collections API in async mode (`action=RESTORE&async=<id>`, then poll
`action=REQUESTSTATUS`), which has no timeout to hit; keep the archives until it's done. A long
`recording` restore can still need `docker compose restart search` afterwards.

## Pointing Groovehound at it

Two settings in `config.toml` (or `GROOVEHOUND_MUSICBRAINZ_API_ROOT` and
`GROOVEHOUND_MUSICBRAINZ_INTERVAL`):

```toml
[musicbrainz]
api_root = "http://mirror:5000"   # /ws/2 is added if left out
interval = 0                      # seconds between requests; only for a server of your own
```

- **`interval`** applies to every request to the mirror. musicbrainz.org never gets less than
  1.5 s, whatever is set; a mirror gets 1.5 s too unless you lower it. The background trickles
  (reconciling follows, covers ahead) keep their own randomized pacing, so a short interval
  doesn't make them busier.
- **`contact` is still required.** ListenBrainz and the Cover Art Archive are still the public
  servers, and the mirror gets the same User-Agent.
- **Links still go to musicbrainz.org.** *Open on MusicBrainz* and the like open the real site,
  not the mirror.
- **Search as you type** (phase 7b, after U6) will need a third setting,
  `search_as_you_type = true`, refused unless `api_root` is a server other than musicbrainz.org.
  Setting `api_root` alone doesn't turn it on, since it could be someone else's server.

### When the mirror is down

A failed run pauses MusicBrainz requests for an hour, as with musicbrainz.org, and pages say
MusicBrainz couldn't be reached. **There's no automatic fallback** to musicbrainz.org: work paced
for a mirror at `interval = 0` would land on the public server. Going back is a settings change:
clear `api_root` (and `interval`) and restart.

## Speed

Measured on the mirror described above (12 GiB, DRAM-less NVMe):

- **Track lists** (2026-11-14, from another machine on the network, through Groovehound's own
  client at `interval = 0`): an album page's editions browse and track lookup together took a
  median 0.07–0.16 s, at most 0.39 s, over 52 albums. On musicbrainz.org the same is 2.5–4 s
  behind the rate limit. That's why Groovehound loads every track list on view instead of
  fetching them ahead of time.
- **Search** (2026-11-05, 95 keystrokes replayed on the mirror's own machine, nothing else
  running, `limit=10`, plain wildcards): p95 35 ms for artists, 91 ms for albums (release
  groups) and 195 ms for recordings; slowest 288 ms. Fast enough to search as you type. Not yet
  measured: the boosted album query 7b plans, and search during replication and indexing.
