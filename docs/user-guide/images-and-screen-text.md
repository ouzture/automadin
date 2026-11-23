# Images and screen text

Visual automation is useful when a target has no reliable element model, but it
is sensitive to scale, theme, rendering, and screen layout. Start with target-
native browser locators when available and use images where they are genuinely
the best evidence.

## Create a template image

1. Connect or select the intended target.
2. Open **Vision lab** and capture a fresh screenshot.
3. Crop tightly around a distinctive, stable visual feature.
4. Save it with a grouped name such as `Examples/Browser/example-heading`.
5. Test the match against the same capture and then against a fresh state.

Avoid including usernames, message content, timestamps, or other changing data in
templates. A smaller distinctive crop is usually more reusable than a large
window capture.

## Image commands

**Find Image** records whether a template is present. **Click Image** clicks a
matched template. **If Image** and **While** use presence as control-flow evidence.
Windows-specific image commands limit capture and input to the configured window.

Search regions improve speed and reduce false matches. Coordinate and image
offsets can be expressed in pixels or percentages. Test the flow at every display
scale and resolution you intend to support, and configure flow scaling only after
understanding the reference resolution.

## Screen-text recognition

Configure OCR through **Tools > Screen Text Recognition**. In **Vision lab**, test
the exact region and preprocessing options before adding **Read Screen Text** to a
flow. The command can return text or a parsed number and can use a fixed region or
an image anchor.

OCR is probabilistic. Make regions tight, choose high-contrast text, normalize
expected number formats, and treat uncertain results as a branch or explicit
failure rather than silently continuing.
