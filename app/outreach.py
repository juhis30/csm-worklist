"""Draft outreach for a CSM to review.

Templates, not an LLM: every sentence ties back to a computed fact, and no
draft can hint at a "risk score". The draft is a starting point. Nothing is
sent without the CSM approving it.
"""

from app.features import FEATURE_BY_NAME

WHY = {
    "entered_top10": "Entered your top 10 by expected ARR at risk this week",
    "risk_jumped": "Risk of losing the renewal rose sharply since last week",
    "blind_spot": "Renewal soon, and we have almost no data on this account",
}

# Keyed by the source of the strongest risk driver.
PLAYS = {
    "product": ("Quick check-in on how {name} is using the platform",
                "Hi {first},\n\nI was looking at how your team has been using the platform recently and wanted to "
                "offer a short working session: 30 minutes to make sure the workflows you rely on are set up well, "
                "and to show a couple of things that have helped similar teams.\n\nWould any time next week work?"
                "\n\nBest,\n{csm}"),
    "zendesk": ("Following up on your recent support requests",
                "Hi {first},\n\nI saw your team has had several support requests with us lately. I'd like to make "
                "sure they're fully resolved and that nothing is still getting in your way. Could we find 20 minutes "
                "this week to go through them together?\n\nBest,\n{csm}"),
    "stripe": ("Making billing easier for {name}",
               "Hi {first},\n\nI wanted to check whether anything on the billing side would be easier for you, for "
               "example invoice timing, payment method or plan fit. Happy to sort it out directly.\n\nBest,\n{csm}"),
    "gmail": ("Checking in",
              "Hi {first},\n\nIt's been a little while since we properly caught up. I'd love 20 minutes to hear "
              "how things are going on your side and what you're focused on next.\n\nBest,\n{csm}"),
    "salesforce": ("Planning ahead for {name}'s renewal",
                   "Hi {first},\n\nYour renewal is coming up and I'd like to get time on the calendar to review "
                   "what's been working, what hasn't, and what you'd want from the next year.\n\nBest,\n{csm}"),
}


def draft(kind, driver, account, features, days_to_renewal):
    """-> {reason, to, subject, body}. to/subject/body are None when there's nobody to write to."""
    feature = FEATURE_BY_NAME.get(driver)
    reason = WHY[kind]
    if feature:
        value = features.get(driver)
        reason += f". Strongest driver: {feature.label} = {'missing' if value is None else feature.fmt.format(value)}"
    reason += f". Renewal in {days_to_renewal} days."

    if kind == "blind_spot" or not account["contact_email"]:
        return {"reason": reason + " Find a current contact and confirm the account is live before renewal.",
                "to": None, "subject": None, "body": None}
    if driver == "bounces_180d":
        return {"reason": reason + " Our contact's email bounced: find the new champion first.",
                "to": None, "subject": None, "body": None}

    subject, body = PLAYS[feature.source if feature else "salesforce"]
    first = (account["contact_name"] or "there").split()[0]
    # {csm} is filled with the approving CSM's name at send time.
    return {"reason": reason, "to": account["contact_email"],
            "subject": subject.format(name=account["name"]), "body": body.format(first=first, csm="{csm}")}
