# Cooking and recipes

Two requests, one tool. Whether you invented the recipe or someone sent you
one, the ingredients end up in `shopping_add_recipe` and it decides what the
house actually needs.

**"Give me a quick slow-cooker meal" / "something delicate and Italian."**
Write the recipe yourself — that is your job, not the server's. Then pass its
full ingredient list to `shopping_add_recipe` with `dish=`. Plan it with
`meal_plan` too if they said which night.

**"Here's a recipe" (a link, a photo, a paste).** Pull out the ingredient list
and pass it through the same tool.

**Pass every ingredient, measurements and all.** Not your guess at what is
missing. The tool is the thing that knows what is in the house; filtering the
list before you hand it over defeats the entire mechanism. `2 tbsp extra virgin
olive oil` is fine — quantities and prep notes are stripped for you.

**Always report what it assumed.** The result's `assumed` field is the things
it decided the house already has — salt, pepper, oil. Say so in one short
clause: *"Added spaghetti, guanciale and eggs. Assumed you've got olive oil,
salt and pepper."* That clause is the only chance anyone has to catch a wrong
assumption, and the tool cannot catch them itself — it knows "butter" is not
"peanut butter", but it has no idea whether the vinegar in the cupboard is the
right vinegar.

**"Do we have everything for X?" is `preview=true`.** It works everything out
and writes nothing.

If someone says a staple is wrong — "we never have vinegar in" — fix it:
`pantry_set item=vinegar assumed=false`. And the reverse for something the
kitchen always has that keeps appearing on the list.
