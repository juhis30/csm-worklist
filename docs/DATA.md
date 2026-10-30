# The data, in plain terms

There is no real customer data in this project. Real records are private, and the exercise asks for synthetic data. So the system **generates a pretend world** of software companies and their customers, built to behave like the real thing, mess included. Think of a flight simulator: fake flights, real lessons.

The generator is [`app/datagen.py`](../app/datagen.py). The data isn't committed to the repo: `python -m app.bootstrap` builds it into `data/worklist.db` in a minute or two, and gives the same result every time (fixed random seed).

## The pretend world

Three made-up software companies, each with its own customers and Customer Success team:

| Company | Customers | CSMs |
|---|---|---|
| Northwind Analytics | 2,600 | 22 |
| Brightpath HR | 1,600 | 13 |
| Cobalt Freight Software | 800 | 7 |

That's **5,000 customers**, about 120 per CSM, as in the brief. They pay from about **$2,000 to $860,000 a year** (average about $40,000). There are **three years of history**, from late 2023 to October 2026, so there are plenty of past renewals to learn from.

## Where the data comes from

Real companies keep customer information in different tools, so the world has five:

| Tool | What it records | Example |
|---|---|---|
| **Salesforce** (customer list) | Who the customer is, what they pay, when they renew, whether they renewed | "Acme pays $50k, renews 1 March, renewed last year" |
| **Product usage** | Each time someone uses the software | "Someone at Acme ran a report on Tuesday" |
| **Zendesk** (support) | Help requests and satisfaction ratings | "Acme filed an urgent ticket", "rated support 1 out of 5" |
| **Stripe** (billing) | Payments | "Acme's card payment failed" |
| **Gmail** | Who emailed whom and when, **never the content** | "The CSM emailed Acme twice with no reply" |

Together that's about **236,000 records**.

## Each customer has a hidden "mood"

Every pretend customer has a **secret health level** that changes week to week. It is **never stored anywhere the system can see**. It only shapes how the customer behaves:

- **Happy customers** use the product a lot, rarely complain, pay on time and reply to emails.
- **Unhappy customers** use it less, complain more, miss payments and stop replying.
- **Sudden setbacks** happen now and then, like the main contact leaving the company, after which emails to them start bouncing.

At **renewal time**, whether a customer stays or leaves depends on their mood over the previous couple of months.

**Why a hidden mood instead of rules?** If we wrote rules like "more than 5 complaints means they leave", the system would just rediscover our rules, which proves nothing. With a hidden mood, it has to **read the clues**, the way a CSM guesses how a customer feels from what they do.

**3% of renewals are lost for no visible reason**, such as budget cuts or an acquisition. No system could predict those, just like in real life.

Out of about **7,900 renewals** in three years, about **1,300 were lost**, roughly 1 in 6.

## Mess added on purpose

Real data is never clean, so this data isn't either:

- **Late arrivals:** some records arrive days or weeks after the event, and some arrive in a later catch-up batch.
- **Duplicates:** the same event is sometimes delivered twice.
- **Inconsistent formats:** each tool writes dates and customer names its own way ("WWW.ACME.COM" vs "acme.com").
- **Missing details:** some customers have no recorded price, website or contact person.
- **Junk:** questions from non-customers, test accounts, personal Gmail addresses and impossible dates.

The system cleans this up. Of about 236,000 records, about **217,000 are usable**. The rest are set aside **with a reason**, never silently dropped: for example, about 6,400 usage records from test or unknown accounts and about 700 with broken dates. If the customer list gets fixed later, set-aside records are picked up again.

## The three companies differ on purpose

| Company | Setup | What it tests |
|---|---|---|
| **Northwind** | Everything connected | The "good data" case |
| **Brightpath** | Never connected Gmail | Working with no email history at all |
| **Cobalt** | Tracks usage for only 60% of customers, bills mostly outside Stripe | Working with big gaps |

The system doesn't pretend to know what it doesn't. Customers it knows too little about go on a separate **blind spots** list instead of being ranked as if they were fine.

## How the data is stored

Everything lives in one SQLite file, `data/worklist.db` (layout in [`app/schema.sql`](../app/schema.sql)). The data passes through three stages, like mail being sorted:

| Stage | Table | Plain meaning |
|---|---|---|
| 1. Arrived | `raw_events` | Everything exactly as the tools sent it, untouched |
| 2. Cleaned | `events` | Records matched to a customer, in one consistent format, duplicates removed |
| 2. Set aside | `rejected_events` | Records that couldn't be used, each with the reason why |

Every record keeps **two times**: when it happened and when we received it. That lets the system test itself fairly on past weeks, using only what it would really have known on each Monday.

The other tables hold who's who and what the system produced:

| Table | Plain meaning |
|---|---|
| `tenants` | The three companies and which tools each one has connected |
| `users` | The CSMs and team leaders who log in |
| `accounts` | The customers, as listed in Salesforce |
| `model_versions` | Each version of the prediction model, and how well it tested |
| `agent_runs` | A log of every weekly run: what it did and whether it succeeded |
| `scores` | Each customer's risk each week, and the reasons behind it |
| `tasks` | To-dos for CSMs, with drafted emails |
| `outbox` | Emails a CSM approved (nothing is actually sent in the demo) |
| `meta` | Small settings, such as the date the simulation has reached |

## In one sentence

A realistic pretend world of 5,000 customers whose hidden moods leave messy, incomplete clues across five tools, used to check whether the system can read those clues to spot who's about to leave. It can, about twice as well as guessing.
