# Solution brief: CSM Worklist

*Written before any code. One page.*

## What the actual problem is

The VP asked for a health-score dashboard with red / yellow / green, and automatic emails to red accounts. Neither of those is the problem.

**CSMs don't know where to spend their week.** Each CSM owns about 120 accounts and can have meaningful conversations with maybe 10 to 15 of them a week. The Monday question is: *"which ten, why, and what do I say?"*

- **The signals were there, but nobody was looking.** Both lost renewals showed warning signs in Zendesk and product usage. Checking 120 accounts across five tools takes all week, so nobody does it.
- **No data looks the same as healthy.** An account with no tickets and no usage data looks fine in a spreadsheet. Often, silence is the risk.
- **Urgency depends on money and timing, not only health.** A struggling account renewing in 11 months matters less this week than a lukewarm one renewing in 6 weeks, and a $300k account matters more than a $5k one.

## What we'll build

A **weekly ranked worklist for each CSM**, kept current by an agent that runs on its own:

1. **All five sources from day one** (Salesforce, Zendesk, product events, Stripe, Gmail) normalised into one event stream. Every source is messy in its own way, so the work is in matching events to accounts, removing duplicates and handling late data, not in connecting more tools later. For Gmail we use **metadata only** (who, when, direction, bounces), never message content.
2. **A model that predicts P(the account's next renewal is lost)**, trained on past renewal outcomes from Salesforce. This is the outcome the VP actually cares about, and it has a real label.
3. **A ranked list, not colours.** Accounts are ordered by **expected ARR at risk** = P(lost) × ARR, and each one shows its renewal date and the top reasons behind its score.
4. **A separate "blind spots" list:** accounts renewing soon that we have too little data to score. They're never hidden at the bottom of the ranking.
5. **A weekly agent** that pulls in new data, rescores every account, compares with last week, and opens a task for the CSM with a **drafted email**. The CSM edits and approves it, or dismisses it.
6. **Traceability.** Every number links to the model version, the inputs and the raw events behind it.

**Build order:** data foundation → features and backtest → worklist → agent. A trustworthy ranked list without automation is more useful than automation acting on an unvalidated score.

## Where we disagree with the request

| Request | Concern | What we'll do instead |
|---|---|---|
| "If red, the system emails the customer" | Any model is wrong often. An automated "are you OK?" sent to a happy customer does damage, and the CSM owns the relationship. | The agent **drafts** the email and opens a task. A CSM approves before anything is sent. |
| Red / yellow / green | Colour cut-offs are arbitrary, a 3-colour scale throws away the difference between a $5k and a $300k account, and green hides "we have no data". | **A ranked list** by expected ARR at risk, with the probability and reasons shown. Accounts with too little data go in a separate blind-spots list. |
| A dashboard | A dashboard has to be read. A worklist tells you what to do. | "My week" is the main screen. Account pages are there to drill into. |

## What we need from the business

- **A definition of churn.** Does only a lost logo count, or do downgrades too? From what date?
- **Renewal history with dates and outcomes.** These are the training labels.
- **The two lost accounts**, so we can check whether the system would have flagged them, and how early.
- **CSM capacity and account ownership.** Is 10 to 15 conversations a week right?
- **Data-sharing terms per customer organisation.** Can one customer's data help train a model used by another? We assume not.
- **Email policy.** Who can send what, and does anything need legal review?
- **Gmail access.** Do customers consent to metadata access?
- **Playbooks.** What a CSM should actually do for each kind of risk.
