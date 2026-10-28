"""Synthetic world: tenants, CSMs, accounts, and raw events from five sources.

How it works
------------
Each account has a hidden weekly health value h: a random walk that drifts
back toward the account's own baseline, with occasional shocks (a champion
leaves, a bad outage, a new sponsor). Health is never written to the
database. It only drives:

  * how often each source emits events (usage falls, tickets rise as h drops)
  * what the events say (urgent tickets, bad CSAT, failed payments, no replies)
  * whether the account renews on each contract anniversary

So the model has to recover risk from noisy, partial signals, the same as in
real life. On top of that, 3% of renewals are lost for reasons the data never
shows (budget cuts, acquisitions), which caps how good any model can be.

The mess, on purpose
--------------------
  * late and out-of-order: per-source delivery lag, plus multi-week backfills
  * duplicates: webhook retries and re-syncs re-deliver the same event id
  * inconsistent formats and ids: every source has its own timestamp format
    and its own way of pointing at an account
  * dirty and unusable records: messy CRM domains, missing ARR and contacts,
    prospects, personal billing emails, test workspaces, corrupted timestamps
"""

import json
from datetime import datetime, timedelta, timezone

import numpy as np

from app import config

UTC = timezone.utc
DAY = 86400.0
WEEK = 7 * DAY

TENANTS = [
    {   # Large, everything connected.
        "tenant_id": "northwind", "name": "Northwind Analytics", "accounts": 2600, "csms": 22,
        "integrations": {"salesforce": True, "zendesk": True, "product": True, "stripe": True, "gmail": True},
        "product_coverage": 0.93, "zendesk_coverage": 0.85, "stripe_coverage": 0.85,
    },
    {   # Never connected Gmail.
        "tenant_id": "brightpath", "name": "Brightpath HR", "accounts": 1600, "csms": 13,
        "integrations": {"salesforce": True, "zendesk": True, "product": True, "stripe": True, "gmail": False},
        "product_coverage": 0.88, "zendesk_coverage": 0.80, "stripe_coverage": 0.80,
    },
    {   # Small; product tracking half rolled out; most customers pay by invoice.
        "tenant_id": "cobalt", "name": "Cobalt Freight Software", "accounts": 800, "csms": 7,
        "integrations": {"salesforce": True, "zendesk": True, "product": True, "stripe": True, "gmail": True},
        "product_coverage": 0.60, "zendesk_coverage": 0.75, "stripe_coverage": 0.40,
    },
]

SEGMENTS = {
    #       share, median ARR, seat range, renewal-risk adjustment
    "SMB": (0.60, 9_000, (3, 25), 0.35),
    "MM": (0.30, 42_000, (20, 150), 0.0),
    "ENT": (0.10, 190_000, (150, 1200), -0.45),
}
UNEXPLAINED_CHURN = 0.03

# How often the same event id is delivered twice, per source.
DUPLICATE_RATE = {"stripe": 0.05, "salesforce": 0.04, "product": 0.02, "gmail": 0.02, "zendesk": 0.01}
CORRUPT_TIMESTAMP_RATE = 0.003

ADJ = ["Granite", "Harbor", "Summit", "Blue", "Maple", "Iron", "Silver", "Cedar", "North", "Bright",
       "Copper", "Pioneer", "Atlas", "Lumen", "Coastal", "Prairie", "Vertex", "Oak", "Delta", "Quarry",
       "Redwood", "Sterling", "Beacon", "Willow", "Falcon", "Evergreen", "Orbit", "Keystone", "Juniper", "Tidal"]
NOUN = ["Dental", "Logistics", "Labs", "Health", "Partners", "Foods", "Robotics", "Legal", "Media", "Energy",
        "Capital", "Clinics", "Freight", "Studios", "Systems", "Retail", "Builders", "Analytics", "Bank", "Travel"]
SUFFIX = ["Inc", "LLC", "Group", "Co", "Ltd", "Holdings"]
FIRST = ["Ana", "Ben", "Chloe", "Dev", "Elena", "Farah", "Gus", "Hana", "Ivan", "Jia", "Kofi", "Lena",
         "Marco", "Nia", "Omar", "Priya", "Quinn", "Rosa", "Sam", "Tara", "Uma", "Victor", "Wen", "Yusuf", "Zoe"]
LAST = ["Okafor", "Silva", "Chen", "Patel", "Novak", "Haddad", "Kim", "Moreau", "Garcia", "Ito", "Larsen",
        "Mensah", "Rossi", "Singh", "Walker", "Nguyen", "Kowalski", "Ahmed", "Dubois", "Park"]
PRODUCT_EVENTS = ["login", "report_viewed", "dashboard_created", "export", "integration_sync", "alert_configured"]
TIMEZONES = [-8, -7, -5, -4, 0, 1, 2, 5.5, 8, 10]


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(ts):
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def rfc2822(ts, tz_hours):
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=tz_hours))).strftime("%a, %d %b %Y %H:%M:%S %z")


class World:
    def __init__(self, seed=config.SEED, scale=config.SCALE):
        self.rng = np.random.default_rng(seed)
        self.scale = scale
        end = config.DATA_END
        self.end_ts = datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp()
        self.start_ts = self.end_ts - config.HISTORY_YEARS * 52 * WEEK   # also a Monday
        self.n_weeks = int((self.end_ts - self.start_ts) // WEEK)
        self.tenants, self.users, self.accounts, self.raw = [], [], [], []
        self.truth = []  # hidden facts for analysing the generator; never loaded into the app
        self._seq = 0

    # ------------------------------------------------------------ plumbing

    def next_id(self, prefix=""):
        self._seq += 1
        return f"{prefix}{self._seq:07d}"

    def pick(self, items):
        return items[self.rng.integers(len(items))]

    def maybe_corrupt(self, value):
        return None if self.rng.random() < CORRUPT_TIMESTAMP_RATE else value

    def lag(self, source):
        """Seconds between an event happening and it reaching us."""
        r = self.rng
        if source == "product":   # streaming, but 3% arrive in a batch backfill days to weeks later
            return r.uniform(2 * DAY, 20 * DAY) if r.random() < 0.03 else r.exponential(600)
        if source == "zendesk":   # polled every few hours
            return r.exponential(3 * 3600)
        if source == "stripe":    # webhooks, near real time
            return r.exponential(30)
        if source == "gmail":     # nightly sync; 2% from a mailbox re-sync weeks later
            return r.uniform(7 * DAY, 30 * DAY) if r.random() < 0.02 else r.uniform(0, DAY)
        return r.uniform(0.2 * DAY, 1.2 * DAY)   # salesforce nightly sync

    def emit(self, tenant, source, source_event_id, payload, occurred_ts):
        ingested = occurred_ts + max(self.lag(source), 1.0)
        self.raw.append((tenant, source, source_event_id, json.dumps(payload), iso(ingested)))
        if self.rng.random() < DUPLICATE_RATE[source]:   # same id again, a bit later
            self.raw.append((tenant, source, source_event_id, json.dumps(payload),
                             iso(ingested + self.rng.uniform(60, 3 * DAY))))

    # ------------------------------------------------------------ world

    def build(self):
        for t in TENANTS:
            self.build_tenant(t)
        return self

    def build_tenant(self, t):
        tid = t["tenant_id"]
        self.tenants.append((tid, t["name"], json.dumps({"integrations": t["integrations"]})))
        n_csms = max(2, round(t["csms"] * self.scale))
        csms = []
        for i in range(n_csms):
            name = f"{self.pick(FIRST)} {self.pick(LAST)}"
            uid = f"{tid}-csm{i + 1:02d}"
            csms.append(uid)
            self.users.append((uid, tid, name, f"{name.split()[0].lower()}.{i + 1}@{tid}.example", "csm"))
        self.users.append((f"{tid}-lead", tid, "VP Customer Success", f"vp@{tid}.example", "leader"))

        used = set()
        for i in range(max(20, round(t["accounts"] * self.scale))):
            name = self.unique_name(used)
            self.build_account(t, f"{tid[:2].upper()}-{i + 1:05d}", name, csms[i % n_csms])

    def unique_name(self, used):
        while True:
            name = f"{self.pick(ADJ)} {self.pick(NOUN)}"
            if self.rng.random() < 0.5:
                name += f" {self.pick(SUFFIX)}"
            if name not in used:
                used.add(name)
                return name

    def health_path(self):
        """Weekly hidden health, plus which weeks had a negative shock."""
        r = self.rng
        baseline = r.normal(0.6, 0.9)          # some accounts are just never happy
        h = np.empty(self.n_weeks)
        shocks = np.zeros(self.n_weeks, dtype=bool)
        cur = baseline + r.normal(0, 0.5)
        for w in range(self.n_weeks):
            jump = 0.0
            if r.random() < 0.006:             # champion leaves, reorg, bad outage
                jump, shocks[w] = -r.uniform(1.0, 2.5), True
            elif r.random() < 0.004:           # new sponsor, successful rollout
                jump = r.uniform(0.5, 1.5)
            cur += 0.05 * (baseline - cur) + r.normal(0, 0.10) + jump
            h[w] = cur
        return baseline, h, shocks

    def week_of(self, ts):
        return min(self.n_weeks - 1, max(0, int((ts - self.start_ts) // WEEK)))

    def build_account(self, t, account_id, name, csm_id):
        r = self.rng
        tid = t["tenant_id"]
        seg = r.choice(list(SEGMENTS), p=[s[0] for s in SEGMENTS.values()])
        _, median_arr, (lo, hi), seg_risk = SEGMENTS[seg]
        seats = int(r.integers(lo, hi + 1))
        arr = round(median_arr * r.lognormal(0, 0.45), -2)
        slug = "".join(ch for ch in name.lower() if ch.isalnum())
        domain = f"{slug}.{self.pick(['com', 'io', 'co', 'net'])}"
        contact = f"{self.pick(FIRST)} {self.pick(LAST)}"
        contact_email = f"{contact.split()[0].lower()}@{domain}"

        # Contracts started up to two years before our data begins, so some
        # accounts have three years of history and some have a few weeks.
        start_ts = r.uniform(self.start_ts - 730 * DAY, self.end_ts - 30 * DAY)
        start_ts -= start_ts % DAY
        has = {
            "product": t["integrations"]["product"] and r.random() < t["product_coverage"],
            "zendesk": t["integrations"]["zendesk"] and r.random() < t["zendesk_coverage"],
            # enterprise customers mostly pay by invoice, outside Stripe
            "stripe": t["integrations"]["stripe"] and r.random() < t["stripe_coverage"] * (0.4 if seg == "ENT" else 1),
            "gmail": t["integrations"]["gmail"],
        }
        workspace_id = f"ws_{r.integers(16 ** 6):06x}" if has["product"] else None

        baseline, h, shocks = self.health_path()

        # Renewal on each anniversary, decided by health over the prior 8 weeks.
        renewals, end_ts, k = [], None, 1
        while (ann := start_ts + k * 365 * DAY) <= self.end_ts:
            if ann > self.start_ts:
                w = self.week_of(ann)
                recent = h[max(0, w - 8):w + 1].mean()
                p_lost = sigmoid(-2.0 - 1.3 * recent + seg_risk + r.normal(0, 0.5))
                lost = r.random() < p_lost or r.random() < UNEXPLAINED_CHURN
                renewals.append((ann, lost))
                if lost:
                    end_ts = ann
                    break
            k += 1

        self.accounts.append((tid, account_id, name, *self.crm_dirt(domain, arr, contact_email, workspace_id),
                              seg, seats, csm_id, day(start_ts), contact))
        self.truth.append({"tenant_id": tid, "account_id": account_id, "segment": seg, "baseline": baseline,
                           "renewals": [(day(a), lost) for a, lost in renewals]})

        a = {"tid": tid, "id": account_id, "domain": domain, "seats": seats, "seg": seg, "arr": arr,
             "contact_email": contact_email, "workspace_id": workspace_id, "has": has}
        first_w = self.week_of(start_ts) if start_ts > self.start_ts else 0
        last_w = self.n_weeks if end_ts is None else self.week_of(end_ts) + 1
        for w in range(first_w, last_w):
            self.emit_week(a, w, h[w], shocks[w])
        for ann, lost in renewals:
            self.emit_renewal(a, ann, lost)
        if has["stripe"]:
            self.emit_billing(a, start_ts, end_ts, h)

    def crm_dirt(self, domain, arr, contact_email, workspace_id):
        """What Salesforce actually holds, as typed by busy humans."""
        r = self.rng
        roll = r.random()
        crm_domain = (f"https://www.{domain}/" if roll < 0.05 else domain.upper() if roll < 0.08
                      else None if roll < 0.10 else domain)
        roll = r.random()
        crm_arr = None if roll < 0.03 else 0.0 if roll < 0.04 else arr
        crm_contact = None if r.random() < 0.04 else contact_email
        crm_ws = workspace_id if (workspace_id and r.random() < 0.93) else None   # 7% never mapped
        return crm_domain, crm_arr, crm_contact, crm_ws

    # ------------------------------------------------------------ sources

    def emit_week(self, a, w, hw, shock):
        r = self.rng
        tid = a["tid"]
        week_ts = self.start_ts + w * WEEK

        if a["has"]["product"]:
            rate = 0.022 * a["seats"] ** 0.55 * 2 * sigmoid(2.0 * hw)
            active_users = max(1, int(a["seats"] * sigmoid(hw)))
            for _ in range(r.poisson(rate)):
                ts = week_ts + r.uniform(0, WEEK)
                payload = {"workspace_id": a["workspace_id"] if r.random() > 0.001 else "ws_test",
                           "event": self.pick(PRODUCT_EVENTS), "user_id": f"u{r.integers(active_users)}",
                           "ts": self.maybe_corrupt(int(ts * 1000))}           # epoch milliseconds
                self.emit(tid, "product", self.next_id("pe_"), payload, ts)

        if a["has"]["zendesk"]:
            for _ in range(r.poisson(0.011 * a["seats"] ** 0.35 * np.exp(-1.2 * hw))):
                self.emit_ticket(a, week_ts + r.uniform(0, WEEK), hw)

        if a["has"]["gmail"]:
            csm_addr = f"csm@{tid}.example"
            for _ in range(r.poisson({"SMB": 0.04, "MM": 0.09, "ENT": 0.2}[a["seg"]])):
                ts, tz = week_ts + r.uniform(0, WEEK), self.pick(TIMEZONES)
                if shock and r.random() < 0.7:       # the champion's mailbox is gone
                    self.emit_mail(tid, "bounce", csm_addr, a["contact_email"], ts, tz)
                    continue
                self.emit_mail(tid, "out", csm_addr, a["contact_email"], ts, tz)
                if r.random() < sigmoid(0.4 + 1.1 * hw):
                    self.emit_mail(tid, "in", a["contact_email"], csm_addr, ts + r.lognormal(9.5, 1.0), tz)
            # customers sometimes write first, more often when unhappy; some from personal addresses
            for _ in range(r.poisson(0.01 * np.exp(-0.6 * hw))):
                sender = a["contact_email"] if r.random() > 0.08 else f"someone{r.integers(999)}@gmail.com"
                self.emit_mail(tid, "in", sender, csm_addr, week_ts + r.uniform(0, WEEK), self.pick(TIMEZONES))

    def emit_ticket(self, a, ts, hw):
        r = self.rng
        ticket_id = int(self.next_id()) + 100000
        p_urgent = min(0.35, sigmoid(-2.6 - 0.9 * hw))
        priority = r.choice(["low", "normal", "high", "urgent"],
                            p=[0.25, 0.55 - p_urgent / 2, 0.20 - p_urgent / 2, p_urgent])
        priority = None if r.random() < 0.05 else str(priority)
        # The org link is whatever the support agent typed, or the requester's email.
        org = self.pick([a["domain"], "www." + a["domain"], a["domain"].upper(), a["contact_email"]])
        if r.random() < 0.04:
            org = f"prospect{r.integers(9999)}.com"      # pre-sales question from a non-customer

        def payload(kind, at, **extra):
            return {"type": kind, "ticket": {"id": ticket_id, "priority": priority, "organization": org},
                    "updated_at": self.maybe_corrupt(iso(at)), **extra}

        self.emit(a["tid"], "zendesk", f"zd{ticket_id}-c", payload("ticket.created", ts), ts)
        if r.random() < 0.92:                             # 8% are never solved
            solved = ts + r.lognormal(np.log(1.5 if priority == "urgent" else 3.0), 0.9) * DAY
            if solved < self.end_ts:
                self.emit(a["tid"], "zendesk", f"zd{ticket_id}-s", payload("ticket.solved", solved), solved)
                if r.random() < 0.35:
                    rated = solved + r.uniform(0.1, 3) * DAY
                    good = r.random() < sigmoid(1.6 + 1.3 * hw)
                    self.emit(a["tid"], "zendesk", f"zd{ticket_id}-r",
                              payload("satisfaction.rated", rated, score="good" if good else "bad"), rated)

    def emit_mail(self, tid, direction, sender, to, ts, tz):
        payload = {"direction": direction, "from": sender, "to": to, "date": self.maybe_corrupt(rfc2822(ts, tz))}
        self.emit(tid, "gmail", self.next_id("msg_"), payload, ts)

    def emit_billing(self, a, start_ts, end_ts, h):
        r = self.rng
        monthly = a["seg"] == "SMB" and r.random() < 0.4
        step = 30.4 * DAY if monthly else 365 * DAY
        amount = int(a["arr"] / (12 if monthly else 1) * 100)
        email = f"billing@{a['domain']}" if r.random() > 0.06 else f"ap.{a['id'].lower()}@gmail.com"
        ts, stop = start_ts, min(self.end_ts, end_ts or self.end_ts)
        while ts < stop:
            if ts >= self.start_ts:
                hw = h[self.week_of(ts)]
                failed = r.random() < 0.02 + sigmoid(-3.2 - 1.3 * hw)
                self.emit_stripe(a, "invoice.payment_failed" if failed else "invoice.paid", ts, amount, email)
                if failed and r.random() < 0.7:          # retry succeeds
                    self.emit_stripe(a, "invoice.paid", ts + r.uniform(2, 6) * DAY, amount, email)
                if r.random() < 0.02 * np.exp(-0.8 * hw):
                    self.emit_stripe(a, "customer.subscription.downgraded", ts + r.uniform(0, 20) * DAY,
                                     int(amount * 0.3), email)
            ts += step

    def emit_stripe(self, a, kind, ts, amount, email):
        payload = {"type": kind, "created": self.maybe_corrupt(int(ts)),
                   "data": {"object": {"customer_email": email, "amount": amount}}}
        self.emit(a["tid"], "stripe", self.next_id("evt_"), payload, ts)

    def emit_renewal(self, a, ann_ts, lost):
        # Losses are often known before the date (notice given); wins get booked late.
        close_ts = min(self.end_ts, ann_ts + (self.rng.uniform(-45, 5) if lost else self.rng.uniform(-5, 25)) * DAY)
        opp_id = self.next_id("006")
        payload = {"Id": opp_id, "AccountId": a["id"], "Type": "Renewal",
                   "StageName": "Closed Lost" if lost else "Closed Won",
                   "Renewal_Date__c": day(ann_ts), "CloseDate": day(close_ts), "Amount": a["arr"]}
        self.emit(a["tid"], "salesforce", opp_id, payload, close_ts)


def load(conn, world: World):
    conn.executemany("INSERT INTO tenants VALUES (?, ?, ?)", world.tenants)
    conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?, ?)", world.users)
    conn.executemany(
        "INSERT INTO accounts (tenant_id, account_id, name, domain, arr, contact_email, workspace_id, "
        "segment, seats, csm_id, contract_start, contact_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        world.accounts)
    # Shuffled, so insertion order says nothing about event time.
    order = world.rng.permutation(len(world.raw))
    conn.executemany("INSERT INTO raw_events (tenant_id, source, source_event_id, payload, ingested_at) "
                     "VALUES (?, ?, ?, ?, ?)", [world.raw[i] for i in order])


def summary(world: World) -> dict:
    """Realism check: volumes, mess and outcomes, as numbers."""
    from collections import Counter
    sources = Counter(r[1] for r in world.raw)
    unique = len({(r[0], r[1], r[2]) for r in world.raw})
    renewals = [lost for a in world.truth for _, lost in a["renewals"]]
    return {
        "accounts": len(world.accounts),
        "raw_events": len(world.raw),
        "unique_events": unique,
        "duplicates": len(world.raw) - unique,
        "by_source": dict(sources.most_common()),
        "renewals_decided": len(renewals),
        "renewal_loss_rate": round(sum(renewals) / len(renewals), 3),
        "crm_missing_domain": sum(a[3] is None for a in world.accounts),
        "crm_missing_or_zero_arr": sum(not a[4] for a in world.accounts),
        "crm_missing_contact": sum(a[5] is None for a in world.accounts),
    }


if __name__ == "__main__":
    for k, v in summary(World().build()).items():
        print(f"{k:26s} {v}")
