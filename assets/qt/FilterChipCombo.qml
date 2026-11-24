pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

FlatCombo {
    id: combo
    property int maxChipWidth: 200
    chip: true
    implicitContentWidthPolicy: ComboBox.ContentItemImplicitWidth
    width: Math.min(maxChipWidth, Math.max(96, implicitContentWidth + leftPadding + rightPadding))
    height: implicitHeight
    focusPolicy: Qt.TabFocus
    typeAhead: true
}
