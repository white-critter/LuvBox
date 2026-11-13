pragma Singleton

import QtQuick

// The one place for colors, spacing, corner radii and type sizes.
// Every screen reads these names; a literal color or size in another file is
// either a picture (canvas strokes, tag tints) or a mistake.
QtObject {
    // Surfaces, from the window up to what floats over it. Panels follow the
    // startup screen: a shade lighter at the top, a hairline, a faint lit edge.
    readonly property color window: "#0e0e0e"
    readonly property color windowTop: "#151414"     // the window is a touch lighter at the top
    readonly property color card: "#171717"          // panels resting on the window (their lower edge)
    readonly property color cardTop: "#1f1e1e"       // and their upper edge
    readonly property color well: "#131313"          // behind pictures and lists inside a card
    readonly property color field: "#1e1e1e"         // inputs and filter chips
    readonly property color fieldDisabled: "#161616"
    readonly property color raised: "#272727"        // quiet buttons
    readonly property color raisedHover: "#333333"
    readonly property color raisedPressed: "#1f1f1f"
    readonly property color hover: "#232323"         // rows and ghost buttons under the pointer
    readonly property color dialog: "#101010"        // dialogs, lower edge
    readonly property color dialogTop: "#1b1a1a"     // and upper edge
    readonly property color edgeLight: Qt.rgba(1, 1, 1, 0.07)   // the lit line along a panel's top
    readonly property color line: "#2a2a2a"
    readonly property color lineStrong: "#3a3a3a"
    readonly property color scrim: "#b3141414"       // badges drawn over a picture

    readonly property color text: "#f4f1f1"
    readonly property color textMuted: "#b9afb1"
    readonly property color textFaint: "#8a8284"
    readonly property color textDisabled: "#776b6e"

    readonly property color accent: "#d93645"
    readonly property color accentTop: "#ea4b5a"     // the main button is lit from above
    readonly property color glow: "#e23a4c"          // the red light behind the heart on the startup screen
    readonly property color accentHover: "#e24b59"
    readonly property color accentPressed: "#b42332"
    readonly property color accentDisabled: Qt.rgba(0.851, 0.212, 0.271, 0.35)
    readonly property color accentSoft: "#2a1519"    // a filter that is on, a chosen tile
    readonly property color accentSoftHover: "#34191e"
    readonly property color accentLine: "#7d2b35"
    readonly property color accentText: "#ffb3ba"
    readonly property color focus: "#ff8791"         // keyboard focus ring
    readonly property color error: "#ff8791"
    readonly property color notice: "#e6bd79"
    readonly property color good: "#8fd3a0"
    // The fill behind a small label in one of those three colors.
    readonly property color errorSoft: "#2f1a1c"
    readonly property color noticeSoft: "#2d2616"
    readonly property color goodSoft: "#1c2a21"
    readonly property color keepFill: "#1f5a34"      // the "keep this one" badge on a duplicate
    // A link in running text.
    readonly property color link: "#ff6977"

    // Tags on a picture, by where they come from: the post's hashtags (green),
    // the AI's categories (blue) and the person's own tags (purple).
    readonly property color hashtagText: "#8fd3a0"
    readonly property color hashtagFill: "#1c2520"
    readonly property color hashtagHover: "#26352c"
    readonly property color hashtagLine: "#2a4030"
    readonly property color aiTagText: "#9cd7ff"
    readonly property color aiTagFill: "#13253a"
    readonly property color aiTagHover: "#1a3550"
    readonly property color aiTagLine: "#2b5680"
    readonly property color userTagText: "#d99fe6"
    readonly property color userTagFill: "#281c2c"
    readonly property color userTagHover: "#35243a"
    readonly property color userTagLine: "#4a2a50"

    // Spacing. Controls in a row sit `gapSmall` apart, blocks `gap` or `gapLarge`.
    readonly property int gapTiny: 4
    readonly property int gapSmall: 8
    readonly property int gap: 12
    readonly property int gapLarge: 16
    readonly property int gapSection: 24
    readonly property int pageMargin: 24
    readonly property int cardPadding: 20
    readonly property int dialogPadding: 24

    // Corners.
    readonly property int radiusSmall: 6
    readonly property int radiusControl: 8
    readonly property int radiusCard: 12
    readonly property int radiusDialog: 16

    // Type sizes in pixels.
    readonly property int fontDisplay: 28    // a big number that is the point of a card
    readonly property int fontTitle: 22      // a screen's title
    readonly property int fontHeading: 18    // a dialog's title, a status headline
    readonly property int fontSection: 15    // a section inside a card or dialog
    readonly property int fontBody: 14
    readonly property int fontSmall: 13
    readonly property int fontCaption: 12
    readonly property int fontTiny: 11

    // Heights.
    readonly property int controlHeight: 36  // buttons, chips, switches
    readonly property int compactHeight: 28  // a button inside a list row or a status line
    readonly property int inputHeight: 40    // text fields and lists
    readonly property int largeHeight: 44    // the full-width main action
    readonly property int titleBarHeight: 60
}
