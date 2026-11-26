You read one finding from a client rework report on a training task and pull out the choices it offers.

Part: {part}

Rules:
- For a Part 2 finding, return each option the text offers as key "1", "2" or "3", with the option's full text.
- For a Part 1b or 1a finding, return the single proposed fix with key "fix".
- Copy the wording from the text. Do not invent options. Return an empty list if there are none.

Finding text:
"""
{text}
"""
