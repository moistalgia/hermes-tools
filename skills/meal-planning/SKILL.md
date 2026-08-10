---
name: meal-planning
description: Plan meals for the week, decide what's for dinner, and turn a plan into a shopping list. Use when someone asks what's for dinner, wants the week planned, asks what they can make with what's in the house, or wants ingredients added to the list. Works through the `state` MCP server.
tags: []
related_skills: []
---

# Meal Planning

Planning meals is an **inventory problem**, not a creativity problem. What is in
the house, who is home, who is cooking, and how much time there is decide almost
everything. Suggesting food without checking those is guessing, and it is why
generic meal suggestions get ignored.

All state goes through the **`state` MCP server**.

## Before suggesting anything

Three calls, always:

| Question | Call |
| --- | --- |
| What is already planned? | `meal_week` |
| What is in the house? | `pantry_low`, and the pantry generally |
| Who is around, and busy? | `appointment_list days=7` |

A weeknight with a 6pm appointment is not a braise. A day nobody is home does
not need a plan at all. `meal_week` also reports which days are empty, so plan
those and leave the rest alone.

## Suggesting

**Two or three options, with a reason attached to each.** A list of ten is a
list nobody picks from.

> Thursday's tight — you've got the vet at 5. Pasta with the sausage in the
> freezer would be quickest. Otherwise the chicken needs using by Friday.

**Reason from what is in the house first.** "The chicken needs using by Friday"
is a real reason. "How about a stir fry?" is not.

**Ask who is cooking when it is not obvious**, and record it with `cook=`. In a
shared household that is half the value of having a plan at all.

**Do not invent recipes with confidence.** If you are giving quantities or
method for something specific, say where it came from, or say that you are
going from memory and the proportions are approximate. Put a real source in
`recipe_ref` when there is one.

## Recording

`meal_plan date=thursday dish="sausage pasta" cook=Sam`

One dish per date and slot; planning over an existing entry replaces it, so say
what you replaced when you do.

Prefer real dates or weekday names over "tomorrow" when writing a whole week —
easier to check back.

## Turning a plan into a list

After planning, work out what is missing and add it:

1. List the ingredients the plan needs.
2. Check them against the pantry.
3. `shopping_add` only what is not already in the house **and** not already on
   the list — `shopping_add` will tell you if something is a duplicate, but it is
   better not to ask.
4. Say what you added, briefly. Not the whole list.

Add `pantry_low` items at the same time. Someone going to the shop should come
back with everything, and staples running out is exactly what that table is for.

**Do not order anything.** There is no purchasing in this system and there
should not be. The list on their phone at the shop is the deliverable.

## Learning what they actually eat

`meal_week` history is a record of what this household really cooks. Use it.

If something has been planned four times in a month, it is a favourite and worth
suggesting again. If something was planned and the notes say it did not go well,
do not suggest it back a week later. When someone says a dish worked or did not,
put that in the meal's `notes` or in a `fact_record` — that is the difference
between a system that learns and one that suggests risotto forever.

## What not to do

**Do not plan the whole week unless asked.** "What's for dinner" is a question
about tonight. Answer it, then offer.

**Do not moralise about food.** No commentary on how healthy, balanced, or
expensive a choice is unless someone asks. This is the fastest way to make a
useful tool annoying.

**Do not give nutrition or medical advice.** If someone mentions a dietary
restriction or allergy, record it with `fact_record` and respect it absolutely
from then on — but that is memory, not advice.
