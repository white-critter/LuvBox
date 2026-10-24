import QtQuick
import QtQuick.Controls
import QtMultimedia

Item {
    id: preview
    property url sourceUrl: ""
    property string mediaType: "photo"
    property bool playing: true
    property bool _released: false
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
    clip: true

    function release() {
        _released = true
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
        _released = !playing
        updatePlayer()
    }
    onMediaTypeChanged: updatePlayer()
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

    Image {
        id: photo
        anchors.fill: parent
        visible: !preview.gifSource && !preview.videoSource
        source: visible ? preview.activeSource : ""
        asynchronous: true
        sourceSize: Qt.size(2048, 2048)
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

    AudioOutput {
        id: audioOutput
        muted: true
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

    Label {
        anchors.centerIn: parent
        width: Math.max(0, parent.width - 32)
        visible: preview.failed
        text: "미리보기를 열지 못했습니다."
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
    }
}
