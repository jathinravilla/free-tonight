---
name: free-tonight
description: Plans a hangout with friends without anyone sharing their whole schedule, budget or food needs; only free/busy times and pooled filters leave each person's Cubbie. Use when the student wants to get dinner, coffee, study or a movie with friends, says the group chat is stuck on "free sometime", or pastes a Free Tonight card (a message with an FT1. code).
---

# Free Tonight

The group chat says "anyone free this week?" and dies at "free sometime". Free Tonight makes the student's Cubbie
their private planning agent. It turns their calendar into a card that shows only when they're free, reads the
cards friends paste back, finds the time that works for the most people, and checks with the student before
anything is locked.

## Privacy rules

- Never put event names, places, class names, grades or the exact budget in anything the student sends to friends.
  The card shows free windows only. Its code carries diet tags, allergens, a budget rounded down to $5, and walk
  time, so friends' Cubbies can pick a place everyone can eat at.
- Near misses ("leave study group 30 min early") are for this student only. Never put them in a group message.
- Cubbie texts only its own student. To reach friends, draft the message and let the student send it.
- Nothing is booked, added to a calendar or sent without the student's yes.

## Setup (first time)

Remember these as "Free Tonight preferences" and reuse them next time. Ask one at a time, with a default:

1. Most they'd spend per hangout (default $20).
2. Diet (vegetarian, vegan, halal, kosher, gluten-free, dairy-free) and allergies (peanut, tree-nut, shellfish,
   fish, egg, soy, dairy, gluten, sesame). Default: none.
3. How far they'll walk (default 20 minutes) and hangout hours (default 11am to 11:30pm).
4. Any commitments they could trim if it gets everyone together (for example "study group, 30 min").

## 1. Make the student's card

When the student wants to plan something:

1. Ask what (dinner, lunch, coffee, study, movie, games) and when (today, tomorrow, weeknights, this week,
   weekend). Suggest dinner, this week.
2. Read the student's Google Calendar events for those days. Write them to `events.json` as
   `[{"start": "2026-10-27T18:00", "end": "2026-10-27T20:00", "title": "Orgo study group"}]` in local time. Add
   `"flex_minutes": 30` to events the student said they could trim. If Google isn't connected, ask them to
   connect it, or to type their week like `MWF 9-10:15 CS 201; Tue 6-8pm study group (flex 30)` and pass that as
   `--schedule` instead of `--events`.
3. Run:

   ```bash
   python3 scripts/ft_card.py make --name "<first name>" --activity dinner --when "this week" \
     --events events.json --diet "<diet>" --allergies "<allergies>" --budget <budget> --walk <minutes>
   ```

4. Send the output to the student as one message they can forward to the group chat, with one line before it:
   "Forward this to the group. Ask friends to paste theirs back to me, or to their own Cubbie."

## 2. Match friends' cards

When the student pastes one or more messages containing `FT1.` codes:

1. Save everything they pasted to `cards.txt`. Read the student's calendar for the same days into `events.json`
   (or use their typed schedule).
2. Run:

   ```bash
   python3 scripts/ft_card.py match --name "<first name>" --activity dinner --events events.json \
     --cards cards.txt --diet "<diet>" --allergies "<allergies>" --budget <budget> --walk <minutes>
   ```

   It prints JSON: `best` (up to 3 times with who can come), `pooled_filters` (diets to serve, allergens to avoid,
   price cap, walk limit, with no names attached) and `private_near_misses_for_this_student_only`.
   Exit code 3 means no time works for 2 or more people: say so and suggest a wider range or a shorter hangout.
3. If a near miss would add a person, ask the student privately first:
   "If you left Orgo study group 30 min early on Tuesday, all 5 of you could do 7:30. Want that?"
   If yes, use that slot.
4. Search the web for places near campus open for the whole slot that serve every diet in `pooled_filters`, can
   avoid every allergen in it, cost at most `price_cap` per person and are within `max_walk_min`. Pick the best
   two by rating.
5. Text the student the top option in three short lines (time, place, who's in), then the runner-up, and ask:
   "Lock in option 1?"

## 3. Lock it in

After the student says yes:

1. Add the event to the student's Google Calendar with the place as the location and a reminder an hour before.
2. Draft the group message for the student to send:

   ```text
   🍜 Dinner Tue 7:30pm
   📍 Halal Bros Grill (about $10)
   🙋 Maya, Jordan, Priya, Leo, Sam
   Reply in or out
   ```

3. Offer a check-in an hour before ("Leaving for dinner in 15?") and, afterwards, to split the bill from a receipt
   photo: total per person, with tax split by what each person ordered and tip on each subtotal.

## Replies

- Short plain text, one idea per message, links on their own line.
- Lead with the answer: "Tuesday 7:30 works for all 5."
- Never paste the JSON to the student.

## When something fails

| Problem | What to do |
| --- | --- |
| Google Calendar not connected | Ask them to connect it, or to type their week (`--schedule`) |
| Exit code 2 (bad input) | Read the message, fix the file or flag, run once more |
| Exit code 3 (no overlap) | Say so; suggest "this week" instead of "tomorrow", or a shorter hangout |
| A pasted message has no `FT1.` code | Ask the friend to forward their whole card message |
| No place fits every filter | Say which filter is the blocker (for example price) without saying whose it is, and offer the closest option |
