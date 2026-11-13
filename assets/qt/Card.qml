import QtQuick

// A raised panel, drawn like the card of the startup screen: a shade lighter at
// the top than at the bottom, a hairline around it and a faint lit upper edge.
// `glow` adds that screen's red light, falling in from the top.
Rectangle {
    id: card
    // 0 for none; around 0.1 is a hint of red, 0.25 is the startup screen.
    property real glow: 0
    // Where along the top edge the light comes from (0 left, 1 right).
    property real glowAt: 0.5
    property color topColor: Theme.cardTop
    property color bottomColor: Theme.card
    radius: Theme.radiusCard
    border.width: 1
    border.color: Theme.line
    gradient: Gradient {
        GradientStop { position: 0; color: card.topColor }
        GradientStop { position: 1; color: card.bottomColor }
    }
    Glow {
        visible: card.glow > 0
        // Kept off the rounded corners, where it would spill past the outline.
        x: card.radius
        y: 1
        width: card.width - 2 * card.radius
        height: Math.min(card.height - 2, Math.max(120, card.width * 0.42))
        strength: card.glow
        focusX: card.glowAt
    }
    // The lit edge: one hairline just inside the top border.
    Rectangle {
        x: card.radius
        y: 1
        width: card.width - 2 * card.radius
        height: 1
        color: Theme.edgeLight
    }
}
