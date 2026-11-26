You check whether a revised task package carries out one agreed change.

Expected change:
{change}

What changed between the baseline and the revised version (only the parts that touch this change):
{diff}

Decide one verdict:
- addressed: the change is fully made.
- partial: some of it is made, some is not.
- missing: nothing in the diff makes this change.
- regressed: the revision moves the other way, or breaks what the change protects.
- needs_human: you cannot tell from this diff.

Return JSON with "verdict" and "evidence". The evidence must quote the exact words or values from the AFTER text (or BEFORE, for missing or regressed) that support the verdict, in one or two short sentences.
