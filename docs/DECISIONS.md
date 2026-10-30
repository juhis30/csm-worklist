# Decisions: edge cases, trade-offs, and details

## Edge cases I anticipated

**Data arriving late, twice or broken**
- **Late data.** Every event has two timestamps: when it happened and when we received it. Features only count events that had both happened *and* reached us by that Monday. Without the second condition, a backtest would quietly use data the live system never had, and the reported accuracy would be inflated.
- **Duplicates.** Webhook retries and re-syncs deliver the same event again. Only the first delivery counts.
- **Events I can't match to an account.** Unknown domains, unmapped workspaces and personal email addresses are rejected with a reason, never dropped silently. These rejections are retried on every run, so fixing the CRM recovers the events. A recovered event counts as received on the day it was recovered, which keeps backtests honest.
- **Broken clocks.** An event that claims to have happened more than a day after we received it is rejected.

**No data is not the same as healthy**
- An account with no tickets and no usage looks fine in a spreadsheet. Each source has an explicit "no data" input, so the model learns what silence means instead of reading it as zero risk.
- Accounts renewing soon with too little evidence go in a separate **blind-spots list**, instead of sitting quietly at the bottom of the ranking.

**A broken feed looks exactly like customers going quiet.** If a connected source stops sending, every account would appear to drop off at once. The agent checks for sources that have gone silent before it scores anything, so a dead integration doesn't set off a wave of false alarms.

**Failures during the weekly run**
- **Re-runs.** The database allows one run per organisation per week. Re-running a week that succeeded does nothing. A failed week is cleared and redone.
- **Crashes.** A week's work is a single transaction, so a crash never leaves half a week of scores. The run record is saved first, so a failure stays visible.
- **One organisation failing** doesn't stop the others.
- **Too little data to train a model.** The run fails visibly instead of publishing scores.

**Retraining can make things worse.** The model retrains every 4 weeks, once there are at least 20 new outcomes. The new model only replaces the old one if it isn't clearly worse on held-out data, and the decision is logged either way.

**Tasks that no longer matter.** A task closes automatically when its renewal date has passed or nobody has acted on it for 4 weeks. Before I added this, 22 open tasks were about renewals that had already happened, and they were also blocking new tasks for those accounts.

**Data leaking between customer organisations.** The web app can only reach tenant data through one function that injects the logged-in user's tenant ID. A query that forgets the tenant filter fails loudly instead of returning another organisation's rows. Each organisation also gets its own model, trained only on its own data.

**Deploying with dev secrets.** On Render, a missing token or session secret fails the deploy instead of falling back to the dev defaults. A test covers this.

## Trade-offs I chose between, and why

| Decision | Chose | Over | Why |
|---|---|---|---|
| Output | Ranked list by **P(lost) × ARR** | Red / yellow / green | Colour cut-offs are arbitrary. Three colours throw away the difference between a $5k and a $300k account, and green hides "we have no data". |
| Customer emails | **Agent drafts, CSM approves** | Automatic send | Any model is often wrong. An automated "are you OK?" to a happy customer does damage, and the CSM owns the relationship. |
| What to predict | **The next renewal being lost** | A health score | It's the outcome the VP cares about, and Salesforce has real labels for it. A health score has no ground truth. |
| Model | **Logistic regression**, one per organisation | Gradient boosting | Each input's contribution adds up exactly to the score shown, so "why is this account at the top?" has a precise answer. Any score can be recomputed by hand from stored JSON. It still beats the rule baseline everywhere (AUC 0.61–0.68 vs 0.53–0.56, about 2× lift in the top 10%). |
| Baseline | Compared against **a sensible spreadsheet rule** | Random | Beating random proves little. The real question is whether this beats what a good CSM would already do. |
| Draft emails | **Templates** | An LLM | Every sentence ties back to a computed fact. No draft can hint at a "risk score" or claim data we don't have. |
| Active-users input | **Dropped** | Keeping it | It duplicated event counts, flipped the usage effect in the wrong direction and added no accuracy. It's still shown to the CSM as context. |
| Blind-spot rule | **Relaxed** for organisations missing a source by design | One strict rule | Otherwise 31% of Cobalt's book was a blind spot because of how Cobalt is set up, not because of anything about the accounts. |
| Account history | **Accepted** a small leak from current CRM values, documented | A full history table | Applying today's ARR and owner to past snapshots leaks a little, but a history table adds complexity that doesn't pay off at this stage. |
| Repeat tasks | **3-week cooldown + expiry** | A first-week cap | This keeps the steady state at about 1 new task per CSM per week. The first-week flood only happens once. |
| Hosting | **Render free tier**, database built at deploy time | A paid instance with a disk | A reviewer always sees the same clean starting state, one week behind, so they can trigger a run themselves. The cost is that clicks are lost when the service sleeps, which the README states. |
| Data page speed | **Left as is**, documented | Saving the stats with each run | It takes about 11 s for the largest organisation. It's a page people rarely visit, and the fix is known. |
| Stack | Python only, server-rendered pages | Separate frontend | One language and one process, with no frontend build step. |

## Details I handled because they mattered, not because they were asked for

- **Synthetic data built on hidden health, not rules.** If losses came from rules like "tickets > 5", the model would simply rediscover the rules. Instead, each account has an unobserved health value that drives every source noisily. 3% of losses have no visible cause, which caps how good any model can be.
- **Deliberately realistic mess.** The generated data has per-source delays, backfills, duplicates, inconsistent formats, dirty CRM records, test workspaces and corrupted timestamps. Lost renewals are often booked up to 45 days early, because the customer gave notice.
- **Out-of-time evaluation.** The model is tested on the 6 months it never saw, measured Monday by Monday, plus a calibration check. A random split would leak the future into training.
- **Business metrics, not just AUC.** I report the share of all lost ARR that sits in our top 20%: 58–74% for the model vs 33–41% for the rule.
- **Gmail metadata only.** The system uses who, when, direction and bounces, never message content.
- **Traceability.** Every number on an account page links to the model version, its inputs and the raw events behind it, including events that arrived too late to count.
- **One definition of "typical", everywhere.** Typical means every input at its training median. The account page shows those medians, each contribution is measured from them (so a value at typical contributes exactly 0), and the calculation starts from the typical account's own score. Nothing is displayed against one reference and computed against another.
- **Explanations never point at filled-in values.** When data was missing, the reason given is "no product data", not a made-up usage number.
- **A regression test stops drafts from claiming usage data we don't have.**
- **Honest uncertainty in the UI.** When there isn't enough held-out evidence that the model beats the rule, CSMs are told the ranking is only directional, and why.
- **A Data page.** It shows delivery counts, rejections by reason and CRM gaps, so problems with the data are visible instead of buried.
- **Questions back to the business.** The [brief](BRIEF.md) lists what I'd need to confirm: the definition of churn, the two lost accounts (to check whether the system would have flagged them early), CSM capacity, data-sharing terms between customer organisations, email policy and Gmail consent.
