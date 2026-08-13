"""The action vocabulary the model is given -- requirement 3.1.

The single most consequential decision in the discovery loop is what the model is
allowed to say. It is *not* "click at (412, 233)" and it is not "here is some CSS".
Every tool below takes a **frame, a role, and an accessible name** -- the exact
vocabulary the artifact stores.

That alignment is the point. Because the model can only express an action the way a
`Locator` expresses it, recording a successful run is a projection rather than a
translation: no inference step has to guess what the model meant, and there is no
class of action the model can take that the artifact cannot represent. A
screenshot-and-coordinates loop would have produced a trajectory that is
fundamentally unrecordable, and the gap would only show up at replay time.

`read_text` doubles as the output-extraction primitive: whatever the model reads on
its way to the goal is a candidate for a declared output, so extraction is
discovered rather than hand-written afterwards.

`finish` and `give_up` are what turn "the loop ended" into a decision the model made
explicitly, which is what a stopping condition needs to be.
"""

from __future__ import annotations

from google.genai import types

#: Shared parameter block. `frame` is required on every action because on this
#: surface an address without a frame identifies nothing -- the top document holds
#: no controls at all.
_TARGET_PROPS = {
    "frame": types.Schema(
        type=types.Type.STRING,
        description=(
            "Name of the frame containing the control, exactly as shown in the "
            "observation header (e.g. 'main'). Use 'top' for the outer document."
        ),
    ),
    "role": types.Schema(
        type=types.Type.STRING,
        description="Accessibility role: button, textbox, combobox, link, cell, row.",
    ),
    "name": types.Schema(
        type=types.Type.STRING,
        description=(
            "The control's accessible name, copied exactly from the observation. "
            "Leave empty ONLY if the control has no name, and then supply "
            "container_name so it can still be identified."
        ),
    ),
    "container_role": types.Schema(
        type=types.Type.STRING,
        description="Optional. Role of an enclosing element, usually 'row'.",
    ),
    "container_name": types.Schema(
        type=types.Type.STRING,
        description=(
            "Optional but REQUIRED when the control itself has no name. The "
            "accessible name of the enclosing row, e.g. 'Nickname'."
        ),
    ),
    "index": types.Schema(
        type=types.Type.INTEGER,
        description=(
            "Optional. Zero-based position among matches inside the container. "
            "Only supply this when several identical controls genuinely exist."
        ),
    ),
}


def _target_schema(extra: dict | None = None, required: list[str] | None = None):
    props = dict(_TARGET_PROPS)
    if extra:
        props.update(extra)
    return types.Schema(
        type=types.Type.OBJECT,
        properties=props,
        required=required or ["frame", "role"],
    )


NAVIGATE = types.FunctionDeclaration(
    name="navigate",
    description=(
        "Load a URL. Use only for the entry point or when explicitly told to; "
        "prefer clicking the interface like a human operator would."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={"url": types.Schema(type=types.Type.STRING)},
        required=["url"],
    ),
)

CLICK = types.FunctionDeclaration(
    name="click",
    description="Click a button, link or other control.",
    parameters=_target_schema(),
)

FILL = types.FunctionDeclaration(
    name="fill",
    description="Type a value into a text field, replacing anything already there.",
    parameters=_target_schema(
        extra={"value": types.Schema(type=types.Type.STRING)},
        required=["frame", "role", "value"],
    ),
)

SELECT = types.FunctionDeclaration(
    name="select",
    description="Choose an option in a dropdown by its visible label.",
    parameters=_target_schema(
        extra={"value": types.Schema(type=types.Type.STRING)},
        required=["frame", "role", "value"],
    ),
)

READ_TEXT = types.FunctionDeclaration(
    name="read_text",
    description=(
        "Read the text of one element and remember it as a named output of this "
        "capability. Use this for every value the goal asks you to retrieve."
    ),
    parameters=_target_schema(
        extra={
            "output_name": types.Schema(
                type=types.Type.STRING,
                description=(
                    "snake_case name for this value in the capability's return "
                    "contract, e.g. savings_balance."
                ),
            )
        },
        required=["frame", "role", "output_name"],
    ),
)

FINISH = types.FunctionDeclaration(
    name="finish",
    description=(
        "Call this ONLY when the goal is fully achieved and every requested value "
        "has been read with read_text."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "success_condition": types.Schema(
                type=types.Type.STRING,
                description=(
                    "A short sentence describing how someone could confirm this "
                    "flow reached its goal, e.g. 'the member record shows a "
                    "SAVINGS row'."
                ),
            ),
            "summary": types.Schema(
                type=types.Type.STRING,
                description="One sentence on what this flow accomplishes.",
            ),
        },
        required=["success_condition", "summary"],
    ),
)

GIVE_UP = types.FunctionDeclaration(
    name="give_up",
    description=(
        "Call this if the goal cannot be achieved -- the data is not there, access "
        "is denied, or the application is failing. Do not guess or invent values."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "reason": types.Schema(type=types.Type.STRING),
            "observed": types.Schema(
                type=types.Type.STRING,
                description="What the screen actually says.",
            ),
        },
        required=["reason"],
    ),
)

TOOLSET = types.Tool(
    function_declarations=[
        NAVIGATE,
        CLICK,
        FILL,
        SELECT,
        READ_TEXT,
        FINISH,
        GIVE_UP,
    ]
)

SYSTEM_INSTRUCTION = """\
You are operating a legacy bank back-office application through its accessibility
tree, the way a trained human operator would. You cannot see pixels. Each turn you
receive a text rendering of every frame on screen, listing controls as
`role "accessible name"`.

Rules, in order of importance:

1. Act only on controls that appear in the CURRENT observation. Never guess a
   control that "should" be there. If what you expected is missing, read the
   observation again and respond to what is actually on screen.
2. Copy accessible names EXACTLY, including capitalisation and punctuation.
3. Always give the `frame` for a control. Most working screens are in frame
   'main'; the outer document usually holds nothing you can act on.
4. If a control has no accessible name (shown as a bare role with no quoted text),
   supply `container_role` and `container_name` instead -- typically the row whose
   label sits beside it.
5. Read every value the goal asks for using `read_text`, giving each a clear
   snake_case `output_name`. A goal is not achieved until its values are read.

5a. CRITICAL -- identify controls by what they ARE, never by the data they
   CONTAIN. This flow will be re-run later for different members, where every
   value on screen is different but the layout is the same.
     * NEVER use a balance, an account number, a date or a person's name as a
       `name` or `container_name`. Those change on the next run.
     * To read a cell inside a table row, identify the ROW by the shortest text
       that says what kind of row it is -- an account type like "SAVINGS", or a
       label like "Nickname" -- and give the cell's `index` (0-based) within it.
     * Ask yourself before every read: "if this member had a different balance,
       would my target still be found?" If not, choose a different anchor.
6. Take ONE action per turn and check the result before continuing.
7. When the goal is achieved, call `finish`. If it cannot be achieved, call
   `give_up` and say what the screen shows. Never fabricate a value.

You are being watched by a safety layer that will refuse actions outside an
allowlist. If an action is refused, do not retry it -- choose a different approach.
"""
