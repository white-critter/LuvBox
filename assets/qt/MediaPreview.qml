import QtQuick
import QtQuick.Controls
import QtQuick.Window
import QtMultimedia

Item {
    id: preview
    property url sourceUrl: ""
    property string mediaType: "photo"
    property bool playing: true
    property bool muted: true
    property int sourcePixelWidth: 0
    property int sourcePixelHeight: 0
    property int sourceRawWidth: 0
    property int sourceRawHeight: 0
    property string missingNote: ""
    property bool externalBusy: false
    property bool _released: false
    property real _manualZoom: 1
    property real _panX: 0
    property real _panY: 0
    property bool _originalResolution: false
    property bool _nativeMode: false
    readonly property real _minimumZoom: 0.05
    readonly property real _maximumZoom: 16
    readonly property real _pixelRatio: Math.max(1, Screen.devicePixelRatio)
    readonly property bool _safeOriginal: !gifSource && !videoSource
        && sourcePixelWidth > 0 && sourcePixelHeight > 0
        && sourcePixelWidth <= 16384 && sourcePixelHeight <= 16384
        && sourcePixelWidth * sourcePixelHeight <= 16000000
        && sourceRawWidth > 0 && sourceRawHeight > 0
        && sourceRawWidth <= 16384 && sourceRawHeight <= 16384
        && sourceRawWidth * sourceRawHeight <= 16000000
        && sourceRawWidth * sourceRawHeight === sourcePixelWidth * sourcePixelHeight
    readonly property real _paintedWidth: videoSource ? videoOutput.contentRect.width
        : gifSource ? animation.paintedWidth : photo.paintedWidth
    readonly property real _paintedHeight: videoSource ? videoOutput.contentRect.height
        : gifSource ? animation.paintedHeight : photo.paintedHeight
    readonly property real _fitPixelScale: _safeOriginal && _paintedWidth > 0
        ? _paintedWidth * _pixelRatio / sourcePixelWidth : 0
    readonly property real _fitReduction: _safeOriginal
        ? Math.min(1, 2048 / Math.max(sourceRawWidth, sourceRawHeight)) : 1
    readonly property int _fitRawWidth: _safeOriginal ? Math.max(1, Math.round(sourceRawWidth * _fitReduction)) : 2048
    readonly property int _fitRawHeight: _safeOriginal ? Math.max(1, Math.round(sourceRawHeight * _fitReduction)) : 2048
    readonly property int _decodeWidth: _originalResolution && _safeOriginal ? sourceRawWidth : _fitRawWidth
    readonly property int _decodeHeight: _originalResolution && _safeOriginal ? sourceRawHeight : _fitRawHeight
    readonly property real _nativeZoom: _fitPixelScale > 0 ? 1 / _fitPixelScale : 1
    readonly property real zoomFactor: _nativeMode
        ? Math.max(_minimumZoom, Math.min(_maximumZoom, _nativeZoom)) : _manualZoom
    readonly property real zoomPercent: 100 * zoomFactor * (_fitPixelScale > 0 ? _fitPixelScale : 1)
    readonly property bool canActualSize: ready && _safeOriginal && _nativeZoom >= _minimumZoom
        && _nativeZoom <= _maximumZoom
    readonly property bool canZoomIn: ready && zoomFactor < _maximumZoom - 0.0001
    readonly property bool canZoomOut: ready && zoomFactor > _minimumZoom + 0.0001
    readonly property real _maximumPanX: Math.max(0, (_paintedWidth * zoomFactor - width) / 2)
    readonly property real _maximumPanY: Math.max(0, (_paintedHeight * zoomFactor - height) / 2)
    readonly property bool canPan: ready && (_maximumPanX > 0.5 || _maximumPanY > 0.5)
    readonly property real panX: _panX
    readonly property real panY: _panY
    readonly property bool gifSource: String(sourceUrl).split("?")[0].split("#")[0].toLowerCase().endsWith(".gif")
    readonly property bool videoSource: mediaType === "video" || (mediaType === "animated_gif" && !gifSource)
    readonly property url activeSource: playing && !_released ? sourceUrl : ""
    readonly property bool failed: String(activeSource).length > 0 && (videoSource
        ? player.error !== MediaPlayer.NoError || player.mediaStatus === MediaPlayer.InvalidMedia
        : gifSource ? animation.status === Image.Error : photo.status === Image.Error)
    readonly property bool loading: String(activeSource).length > 0 && !failed && (videoSource
        ? player.mediaStatus === MediaPlayer.LoadingMedia || player.mediaStatus === MediaPlayer.StalledMedia
        : gifSource ? animation.status === Image.Loading : photo.status === Image.Loading)
    readonly property bool ready: String(activeSource).length > 0 && !failed && (videoSource
        ? player.mediaStatus === MediaPlayer.LoadedMedia || player.mediaStatus === MediaPlayer.BufferedMedia
        : gifSource ? animation.status === Image.Ready : photo.status === Image.Ready)
    readonly property bool unavailable: !loading && !externalBusy && !ready
        && (failed || (playing && !_released && String(activeSource).length === 0))
    clip: true

    function clampPan() {
        _panX = Math.max(-_maximumPanX, Math.min(_maximumPanX, _panX))
        _panY = Math.max(-_maximumPanY, Math.min(_maximumPanY, _panY))
    }

    function zoomAt(factor, x, y) {
        if (!ready || !isFinite(factor))
            return
        const previous = zoomFactor
        const previousPanX = _panX
        const previousPanY = _panY
        const next = Math.max(_minimumZoom, Math.min(_maximumZoom, factor))
        _nativeMode = false
        _manualZoom = next
        _panX = (x - width / 2) - (x - width / 2 - previousPanX) * next / previous
        _panY = (y - height / 2) - (y - height / 2 - previousPanY) * next / previous
        clampPan()
    }

    function zoomIn() { zoomAt(zoomFactor * 1.25, width / 2, height / 2) }
    function zoomOut() { zoomAt(zoomFactor / 1.25, width / 2, height / 2) }
    function fit() {
        _nativeMode = false
        _manualZoom = 1
        _panX = _panY = 0
        _originalResolution = false
    }
    function actualSize() {
        if (!canActualSize)
            return
        // Only a validated still photo is decoded at native resolution, and
        // only on this explicit action. Fit/release returns to the small load.
        _originalResolution = true
        _nativeMode = true
        _panX = _panY = 0
    }

    on_MaximumPanXChanged: clampPan()
    on_MaximumPanYChanged: clampPan()
    onSourcePixelWidthChanged: fit()
    onSourcePixelHeightChanged: fit()
    onSourceRawWidthChanged: fit()
    onSourceRawHeightChanged: fit()

    function release() {
        _released = true
        muted = true
        fit()
        player.stop()
        player.source = ""
        if (typeof videoOutput.clearOutput === "function")
            videoOutput.clearOutput()
    }

    function updatePlayer() {
        player.stop()
        player.source = videoSource && playing && !_released ? sourceUrl : ""
        if (String(player.source).length > 0)
            player.play()
    }

    onSourceUrlChanged: {
        muted = true
        fit()
        _released = !playing
        updatePlayer()
    }
    onMediaTypeChanged: {
        muted = true
        fit()
        updatePlayer()
    }
    onPlayingChanged: {
        if (playing) {
            _released = false
            updatePlayer()
        } else {
            release()
        }
    }
    Component.onCompleted: updatePlayer()
    Component.onDestruction: release()

    Item {
        id: mediaCanvas
        objectName: "previewContent"
        width: preview.width
        height: preview.height
        x: preview._panX
        y: preview._panY
        scale: preview.zoomFactor
        transformOrigin: Item.Center

        Image {
            id: photo
            objectName: "previewPhoto"
            anchors.fill: parent
            visible: !preview.gifSource && !preview.videoSource
            source: visible ? preview.activeSource : ""
            asynchronous: true
            sourceSize: Qt.size(preview._decodeWidth, preview._decodeHeight)
            // sourceSize alone may scale a replacement's different aspect ratio
            // beyond the request. A bounded clip also bounds the native output.
            sourceClipRect: preview._safeOriginal
                ? Qt.rect(0, 0, preview._decodeWidth, preview._decodeHeight) : Qt.rect(0, 0, 0, 0)
            cache: false
            fillMode: Image.PreserveAspectFit
            autoTransform: true
        }

        AnimatedImage {
            id: animation
            anchors.fill: parent
            visible: preview.gifSource && !preview.videoSource
            source: visible ? preview.activeSource : ""
            asynchronous: true
            sourceSize: Qt.size(2048, 2048)
            cache: false
            playing: preview.playing && !preview._released
            fillMode: Image.PreserveAspectFit
        }

        VideoOutput {
            id: videoOutput
            anchors.fill: parent
            visible: preview.videoSource && preview.playing && !preview._released
            fillMode: VideoOutput.PreserveAspectFit
        }
    }

    AudioOutput {
        id: audioOutput
        muted: preview.muted
    }

    MediaPlayer {
        id: player
        videoOutput: videoOutput
        audioOutput: audioOutput
        loops: MediaPlayer.Infinite
        onMediaStatusChanged: {
            if (mediaStatus === MediaPlayer.LoadedMedia && preview.playing && !preview._released)
                play()
        }
    }

    BusyIndicator {
        anchors.centerIn: parent
        running: preview.loading
        visible: running
        Accessible.name: "미리보기 불러오는 중"
    }

    Column {
        objectName: "previewMissingBox"
        anchors.centerIn: parent
        width: Math.max(0, Math.min(360, parent.width - 32))
        visible: preview.unavailable
        spacing: 10

        Canvas {
            anchors.horizontalCenter: parent.horizontalCenter
            width: 56
            height: 46
            onPaint: {
                const ctx = getContext("2d")
                ctx.reset()
                ctx.strokeStyle = "#8a8284"
                ctx.fillStyle = "#8a8284"
                ctx.lineWidth = 2
                ctx.lineJoin = "round"
                ctx.lineCap = "round"
                ctx.strokeRect(4, 4, 48, 38)
                ctx.beginPath()
                ctx.arc(17, 16, 4, 0, Math.PI * 2)
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(9, 36)
                ctx.lineTo(22, 23)
                ctx.lineTo(31, 31)
                ctx.lineTo(39, 24)
                ctx.lineTo(47, 36)
                ctx.stroke()
                ctx.strokeStyle = "#d93645"
                ctx.lineWidth = 2.4
                ctx.beginPath()
                ctx.moveTo(8, 8)
                ctx.lineTo(48, 38)
                ctx.stroke()
            }
        }

        Label {
            width: parent.width
            text: preview.failed ? "미리보기를 열지 못했습니다" : "미리보기 없음"
            font.pixelSize: 15
            font.bold: true
            color: "#d4cdcf"
            textFormat: Text.PlainText
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
        }

        Label {
            width: parent.width
            text: preview.missingNote || (preview.failed
                ? "파일을 읽지 못했거나 지원하지 않는 형식입니다."
                : "원본 파일을 찾을 수 없습니다.")
            font.pixelSize: 12
            color: "#9a9294"
            textFormat: Text.PlainText
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
        }
    }

    MouseArea {
        anchors.fill: parent
        enabled: preview.ready
        acceptedButtons: Qt.LeftButton
        preventStealing: true
        cursorShape: preview.canPan ? (pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor) : Qt.ArrowCursor
        property real startX: 0
        property real startY: 0
        property real startPanX: 0
        property real startPanY: 0
        onPressed: function(mouse) {
            if (!preview.canPan) {
                mouse.accepted = false
                return
            }
            startX = mouse.x
            startY = mouse.y
            startPanX = preview._panX
            startPanY = preview._panY
        }
        onPositionChanged: function(mouse) {
            if (pressed && preview.canPan) {
                preview._panX = startPanX + mouse.x - startX
                preview._panY = startPanY + mouse.y - startY
                preview.clampPan()
            }
        }
        onWheel: function(wheel) {
            const amount = wheel.angleDelta.y ? wheel.angleDelta.y / 120 : wheel.pixelDelta.y / 80
            if (amount)
                preview.zoomAt(preview.zoomFactor * Math.pow(1.2, amount), wheel.x, wheel.y)
            wheel.accepted = true
        }
    }

    FlatButton {
        objectName: "previewAudioToggle"
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 8
        visible: preview.mediaType === "video" && preview.videoSource && preview.ready && player.hasAudio
        text: preview.muted ? "소리 켜기" : "소리 끄기"
        Accessible.name: text
        onClicked: preview.muted = !preview.muted
    }
}
