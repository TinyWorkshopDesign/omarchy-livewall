import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Search video wallpapers on Pixabay (or Pexels with a key you already
// have) and drop them into the current theme's backgrounds folder, where
// Omarchy's background engine (owe) plays them. Open with:
//   omarchy-shell shell toggle tinyworkshop.livewall
//
// Network work happens in livewall.py next to this file, started with an
// argument list: no user text ever goes through a shell. Every answer is
// one JSON line. Text that came from the network is only ever shown as
// PlainText.
Item {
  id: root

  property bool opened: false
  readonly property string selfId: "tinyworkshop.livewall"
  property var shell: null
  onShellChanged: {
    if (!root.opened && root.shell && root.shell.openPanelIds
        && root.shell.openPanelIds[root.selfId] === true)
      root.open("{}")
  }

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  readonly property string script: decodeURIComponent(String(Qt.resolvedUrl("livewall.py")).replace(/^file:\/\//, ""))

  // ------------------------------------------------------------------ state
  property bool keyKnown: false
  property bool hasKey: false
  property string theme: ""
  property string providerLabel: "Pixabay"
  property int page: 1
  property int total: 0
  property bool hasNext: false
  property var items: []
  property bool searching: false
  // Messages keep their key and arguments, not built text, so a language
  // switch re-renders them.
  property string errorCode: ""
  property string errorDetail: ""
  property string noticeKey: ""
  property var noticeArgs: []
  property var downloaded: ({})     // id -> true
  property int dlId: 0
  property int dlPct: 0
  // Resolution: "auto" or a height class ("720", "1080", "1440", "2160").
  property string quality: "auto"
  property int autoQuality: 1440
  property var qualities: [720, 1080, 1440, 2160]
  property var monitors: []          // [{name, width, height, need}]

  // ------------------------------------------------------------------ theme
  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color border: Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  property color selBg: Color.menu.selectedBackground
  property color selText: Color.menu.selectedText
  property color accent: Color.accent
  property color urgent: Color.urgent
  readonly property color secondary: Qt.rgba(foreground.r * 0.65 + background.r * 0.35,
                                             foreground.g * 0.65 + background.g * 0.35,
                                             foreground.b * 0.65 + background.b * 0.35, 1)
  property string fontFamily: Style.font.menuFamily

  // ------------------------------------------------------------- open/close
  function open(payloadJson) {
    root.opened = true
    root.clearMessages()
    if (!keyProc.running) keyProc.running = true
    Qt.callLater(function() { root.focusInput() })
  }

  function close() {
    if (!root.opened) return
    root.opened = false
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide(root.selfId)
  }

  function focusInput() {
    if (root.hasKey) searchField.forceActiveFocus()
    else keyField.forceActiveFocus()
  }

  // ---------------------------------------------------------------- helpers
  function parse(line) {
    try { return JSON.parse(line) } catch (e) { return null }
  }

  function clearMessages() {
    root.errorCode = ""
    root.errorDetail = ""
    root.noticeKey = ""
    root.noticeArgs = []
  }

  function setError(r) {
    root.noticeKey = ""
    root.errorCode = String((r && r.code) || "generic").replace(/[^a-z_]/g, "").substring(0, 40)
    root.errorDetail = String((r && r.detail) || "").substring(0, 200)
  }

  function setNotice(key, args) {
    root.errorCode = ""
    root.noticeKey = key
    root.noticeArgs = args || []
  }

  function fill(text, args) {
    var s = text
    for (var i = 0; i < args.length; i++)
      if (s.indexOf("%" + (i + 1)) >= 0) s = s.arg(args[i])
    return s
  }

  readonly property string messageText: {
    if (root.errorCode !== "") {
      var t = strings.t("error." + root.errorCode, "")
      if (t === "") t = strings.t("error.generic")
      return root.fill(t, [root.errorDetail])
    }
    if (root.noticeKey !== "") return root.fill(strings.t(root.noticeKey), root.noticeArgs)
    return ""
  }

  function qualityLabel(q) {
    q = parseInt(q) || 0
    return q >= 2160 ? "4K" : q + "p"
  }

  // Names of the connected monitors that need this height class.
  function monitorsFor(q) {
    var names = []
    for (var i = 0; i < root.monitors.length; i++)
      if (parseInt(root.monitors[i].need) === q) names.push(String(root.monitors[i].name || "").substring(0, 20))
    return names.join(", ")
  }

  function setQuality(q) {
    if (root.quality === q) return
    root.quality = q
    if (!qualityProc.running) {
      qualityProc.command = ["python3", "-I", root.script, "set-quality", q]
      qualityProc.running = true
    }
    // Results come from the 24-hour cache: re-reading them is free.
    if (root.items.length > 0) root.search(root.page)
  }

  function duration(s) {
    var m = Math.floor(s / 60)
    var r = s % 60
    return m + ":" + (r < 10 ? "0" : "") + r
  }

  // ---------------------------------------------------------------- actions
  function search(p) {
    var q = searchField.text.trim()
    if (q === "" || searchProc.running) return
    root.page = p
    root.clearMessages()
    root.searching = true
    searchProc.command = ["python3", "-I", root.script, "search", q, String(p), Commons.I18n.language, root.quality]
    searchProc.running = true
  }

  function download(id, apply) {
    if (dlProc.running) return
    root.dlId = id
    root.dlPct = 0
    root.clearMessages()
    dlProc.command = ["python3", "-I", root.script, "download", String(id), "--quality", root.quality].concat(apply ? ["--apply"] : [])
    dlProc.running = true
  }

  function saveKey() {
    var k = keyField.text.trim()
    if (k === "" || keySetProc.running) return
    root.clearMessages()
    keySetProc.running = true
    keySetProc.write(k + "\n")
    keyField.text = ""
  }

  // -------------------------------------------------------------- processes
  Process {
    id: keyProc
    command: ["python3", "-I", root.script, "key-status"]
    stdout: SplitParser {
      onRead: function(line) {
        var r = root.parse(line)
        if (!r) return
        root.keyKnown = true
        root.hasKey = !!r.hasKey
        root.theme = String(r.theme || "").substring(0, 60)
        if (r.providerLabel) root.providerLabel = String(r.providerLabel).substring(0, 30)
        var q = String(r.quality || "auto")
        root.quality = (q === "auto" || ["720", "1080", "1440", "2160"].indexOf(q) >= 0) ? q : "auto"
        root.autoQuality = parseInt(r.auto) || 1440
        if (Array.isArray(r.qualities)) root.qualities = r.qualities.slice(0, 6).map(function(x) { return parseInt(x) || 0 })
        root.monitors = Array.isArray(r.monitors) ? r.monitors.slice(0, 16) : []
        Qt.callLater(function() { root.focusInput() })
      }
    }
  }

  Process { id: qualityProc }

  Process {
    id: keySetProc
    stdinEnabled: true
    command: ["python3", "-I", root.script, "key-set", "pixabay"]
    stdout: SplitParser {
      onRead: function(line) {
        var r = root.parse(line)
        if (!r) return
        if (!r.ok) { root.setError(r); return }
        root.hasKey = true
        if (r.providerLabel) root.providerLabel = String(r.providerLabel).substring(0, 30)
        root.setNotice("key.saved")
        Qt.callLater(function() { root.focusInput() })
      }
    }
  }

  Process {
    id: searchProc
    stdout: SplitParser {
      onRead: function(line) {
        var r = root.parse(line)
        if (!r) return
        if (!r.ok) { root.setError(r); return }
        root.theme = String(r.theme || root.theme).substring(0, 60)
        if (r.providerLabel) root.providerLabel = String(r.providerLabel).substring(0, 30)
        root.total = Math.max(0, parseInt(r.total) || 0)
        root.hasNext = !!r.next
        var map = Object.assign({}, root.downloaded)
        var list = Array.isArray(r.items) ? r.items.slice(0, 24) : []
        for (var i = 0; i < list.length; i++)
          if (list[i].downloaded) map[list[i].id] = true
        root.downloaded = map
        root.items = list
        grid.positionViewAtBeginning()
        if (list.length === 0) root.setNotice("results.none")
      }
    }
    onExited: function() { root.searching = false }
  }

  Process {
    id: dlProc
    stdout: SplitParser {
      onRead: function(line) {
        var r = root.parse(line)
        if (!r) return
        if (r.progress !== undefined) { root.dlPct = Math.max(0, Math.min(100, parseInt(r.progress) || 0)); return }
        if (!r.ok) { root.setError(r); return }
        var map = Object.assign({}, root.downloaded)
        map[root.dlId] = true
        root.downloaded = map
        if (r.applied) root.setNotice("done.applied", [parseInt(r.width) || 0, parseInt(r.height) || 0])
        else root.setNotice("done.saved", [root.theme])
      }
    }
    onExited: function() { root.dlId = 0 }
  }

  // ------------------------------------------------------------- components
  component PlainText: Text {
    textFormat: Text.PlainText
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
  }

  component TextButton: Rectangle {
    id: btn
    property string label: ""
    property bool enabledState: true
    signal clicked()
    implicitWidth: btnText.implicitWidth + Style.spacing.lg * 2
    implicitHeight: btnText.implicitHeight + Style.spacing.sm * 2
    radius: Style.cornerRadius
    opacity: enabledState ? 1 : 0.5
    color: btnArea.containsMouse && enabledState ? root.selBg : "transparent"
    border.color: root.border
    border.width: 1
    PlainText {
      id: btnText
      anchors.centerIn: parent
      text: btn.label
      color: btnArea.containsMouse && btn.enabledState ? root.selText : root.foreground
    }
    MouseArea {
      id: btnArea
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: btn.enabledState ? Qt.PointingHandCursor : Qt.ArrowCursor
      onClicked: if (btn.enabledState) btn.clicked()
    }
  }

  // A resolution choice. `monitorsText` names the monitors that need it;
  // those options get an accent outline.
  component QualityButton: Rectangle {
    id: qb
    property string label: ""
    property string monitorsText: ""
    property bool selected: false
    signal clicked()
    readonly property bool highlighted: monitorsText !== ""
    implicitWidth: Math.max(qbLabel.implicitWidth, qbMon.visible ? qbMon.implicitWidth : 0) + Style.spacing.lg * 2
    implicitHeight: qbCol.implicitHeight + Style.spacing.sm * 2
    radius: Style.cornerRadius
    color: qb.selected ? root.accent : (qbArea.containsMouse ? root.selBg : "transparent")
    border.color: qb.highlighted || qb.selected ? root.accent : root.border
    border.width: qb.highlighted ? 2 : 1
    Column {
      id: qbCol
      anchors.centerIn: parent
      PlainText {
        id: qbLabel
        anchors.horizontalCenter: parent.horizontalCenter
        text: qb.label
        font.bold: qb.selected
        color: qb.selected ? root.background : (qbArea.containsMouse ? root.selText : root.foreground)
      }
      PlainText {
        id: qbMon
        anchors.horizontalCenter: parent.horizontalCenter
        visible: qb.highlighted
        text: "󰍹 " + qb.monitorsText
        font.pixelSize: Math.max(9, Style.font.caption - 2)
        color: qb.selected ? root.background : root.accent
      }
    }
    MouseArea {
      id: qbArea
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: qb.clicked()
    }
  }

  // ------------------------------------------------------------------ window
  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-tinyworkshop-livewall"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.opened ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    MouseArea { anchors.fill: parent; onClicked: root.close() }

    BorderSurface {
      id: card
      width: Math.min(Style.space(1000), panel.width - Style.gapsOut * 2)
      height: Math.min(Style.space(760), panel.height * 0.85)
      anchors.centerIn: parent
      radius: Style.cornerRadius
      color: root.background
      borderSpec: root.borderSpec
      padding: Style.spacing.panelPadding
      clip: true
      LayoutMirroring.enabled: Commons.I18n.rightToLeft
      LayoutMirroring.childrenInherit: true

      MouseArea { anchors.fill: parent; onClicked: {} }

      ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: Style.spacing.lg

        // ------------------------------------------------------------ header
        RowLayout {
          Layout.fillWidth: true
          Layout.fillHeight: false
          PlainText {
            text: "󰕧  " + strings.t("title")
            color: root.accent
            font.pixelSize: Style.font.title
            font.bold: true
          }
          Item { Layout.fillWidth: true }
          PlainText {
            text: root.fill(strings.t("header.source"), [root.providerLabel, root.theme])
            color: root.secondary
          }
        }

        // ------------------------------------------------------- missing key
        ColumnLayout {
          Layout.fillWidth: true
          Layout.fillHeight: false
          visible: root.keyKnown && !root.hasKey
          spacing: Style.spacing.sm
          PlainText {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: strings.t("key.explain")
            font.pixelSize: Style.font.body
          }
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.sm
            TextField {
              id: keyField
              Layout.fillWidth: true
              password: true
              placeholderText: strings.t("key.placeholder")
              foreground: root.foreground
              accent: root.accent
              onAccepted: root.saveKey()
              Keys.onEscapePressed: root.close()
            }
            TextButton {
              label: keySetProc.running ? strings.t("key.checking") : strings.t("key.save")
              enabledState: !keySetProc.running
              onClicked: root.saveKey()
            }
          }
        }

        // ------------------------------------------------------------ search
        RowLayout {
          Layout.fillWidth: true
          Layout.fillHeight: false
          visible: root.hasKey
          spacing: Style.spacing.sm
          TextField {
            id: searchField
            Layout.fillWidth: true
            placeholderText: strings.t("search.placeholder")
            foreground: root.foreground
            accent: root.accent
            onAccepted: root.search(1)
            Keys.onEscapePressed: root.close()
          }
          TextButton {
            label: root.searching ? strings.t("search.busy") : strings.t("search.button")
            enabledState: !root.searching
            onClicked: root.search(1)
          }
        }

        // -------------------------------------------------------- resolution
        RowLayout {
          Layout.fillWidth: true
          Layout.fillHeight: false
          visible: root.hasKey
          spacing: Style.spacing.sm
          PlainText {
            text: strings.t("quality.label")
            color: root.secondary
          }
          QualityButton {
            label: root.fill(strings.t("quality.auto"), [root.qualityLabel(root.autoQuality)])
            selected: root.quality === "auto"
            onClicked: root.setQuality("auto")
          }
          Repeater {
            model: root.qualities
            delegate: QualityButton {
              required property var modelData
              label: root.qualityLabel(modelData)
              monitorsText: root.monitorsFor(parseInt(modelData))
              selected: root.quality === String(modelData)
              onClicked: root.setQuality(String(modelData))
            }
          }
          PlainText {
            Layout.fillWidth: true
            visible: root.monitors.length > 0
            elide: Text.ElideRight
            wrapMode: Text.Wrap
            maximumLineCount: 2
            text: strings.t("quality.hint")
            color: root.secondary
          }
        }

        // ---------------------------------------------------------- messages
        PlainText {
          Layout.fillWidth: true
          visible: root.messageText !== ""
          wrapMode: Text.Wrap
          text: root.messageText
          color: root.errorCode !== "" ? root.urgent : root.accent
        }

        // ----------------------------------------------------------- results
        GridView {
          id: grid
          Layout.fillWidth: true
          Layout.fillHeight: true
          visible: root.hasKey
          clip: true
          model: root.items
          readonly property int cols: Math.max(2, Math.floor(width / Style.space(230)))
          cellWidth: Math.floor(width / cols)
          cellHeight: Math.floor(cellWidth * 9 / 16) + Style.space(46)
          boundsBehavior: Flickable.StopAtBounds

          delegate: Item {
            id: cell
            required property var modelData
            readonly property int vid: parseInt(modelData.id) || 0
            readonly property bool isDl: root.dlId === vid
            readonly property bool have: root.downloaded[vid] === true
            width: grid.cellWidth
            height: grid.cellHeight

            Rectangle {
              anchors.fill: parent
              anchors.margins: Style.space(5)
              radius: Style.cornerRadius
              color: tileArea.containsMouse ? root.selBg : "transparent"
              border.color: cell.have ? root.accent : root.border
              border.width: 1

              ColumnLayout {
                anchors.fill: parent
                anchors.margins: Style.space(5)
                spacing: Style.space(4)

                Item {
                  Layout.fillWidth: true
                  Layout.preferredHeight: width * 9 / 16
                  Image {
                    anchors.fill: parent
                    // Only local cache files written by livewall.py.
                    source: (typeof cell.modelData.thumb === "string" && cell.modelData.thumb.charAt(0) === "/")
                            ? "file://" + cell.modelData.thumb : ""
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    sourceSize.width: 480
                    smooth: true
                  }
                  Rectangle {
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.margins: Style.space(4)
                    width: durText.implicitWidth + Style.space(8)
                    height: durText.implicitHeight + Style.space(2)
                    radius: Style.space(3)
                    color: Qt.rgba(0, 0, 0, 0.6)
                    PlainText {
                      id: durText
                      anchors.centerIn: parent
                      text: root.duration(parseInt(cell.modelData.duration) || 0) + " · "
                            + (parseInt(cell.modelData.width) || 0) + "×" + (parseInt(cell.modelData.height) || 0)
                      color: "white"
                    }
                  }
                  Rectangle {
                    visible: cell.isDl
                    anchors.fill: parent
                    color: Qt.rgba(0, 0, 0, 0.55)
                    PlainText {
                      anchors.centerIn: parent
                      text: root.dlPct > 0 ? strings.t("tile.downloadingPct").arg(root.dlPct)
                                           : strings.t("tile.downloading")
                      color: "white"
                      font.pixelSize: Style.font.body
                      font.bold: true
                    }
                  }
                }

                PlainText {
                  Layout.fillWidth: true
                  elide: Text.ElideRight
                  text: (cell.have ? "✓ " : "")
                        + (String(cell.modelData.title || "") || strings.t("tile.untitled").arg(cell.vid))
                  color: cell.have ? root.accent : root.foreground
                }
                PlainText {
                  Layout.fillWidth: true
                  elide: Text.ElideRight
                  text: String(cell.modelData.author || "")
                  color: root.secondary
                }
              }

              MouseArea {
                id: tileArea
                anchors.fill: parent
                hoverEnabled: true
                acceptedButtons: Qt.LeftButton | Qt.RightButton
                cursorShape: dlProc.running ? Qt.BusyCursor : Qt.PointingHandCursor
                // Click: download and set. Right-click: download only.
                onClicked: function(ev) { root.download(cell.vid, ev.button === Qt.LeftButton) }
              }
            }
          }
        }

        // Without a key the grid is hidden: keep everything else at the top.
        Item { Layout.fillHeight: true; visible: !root.hasKey }

        // ------------------------------------------------------------ footer
        RowLayout {
          Layout.fillWidth: true
          Layout.fillHeight: false
          visible: root.hasKey
          spacing: Style.spacing.sm
          PlainText {
            Layout.fillWidth: true
            elide: Text.ElideRight
            text: root.items.length > 0
                  ? strings.tn("footer.page", root.total).arg(root.page).arg(Commons.I18n.formatNumber(root.total, 0))
                    + " · " + strings.t("footer.hint")
                  : strings.t("footer.hint")
            color: root.secondary
          }
          TextButton {
            label: strings.t("footer.prev")
            visible: root.page > 1 && root.items.length > 0
            enabledState: !root.searching
            onClicked: root.search(root.page - 1)
          }
          TextButton {
            label: strings.t("footer.next")
            visible: root.hasNext
            enabledState: !root.searching
            onClicked: root.search(root.page + 1)
          }
          TextButton {
            label: strings.t("footer.all")
            onClicked: {
              root.close()
              // Same action as the menu's Style > Background entry.
              Quickshell.execDetached(["bash", "-c",
                "background=$(omarchy-theme-bg-switcher); [[ -n $background ]] && omarchy-theme-bg-set \"$background\""])
            }
          }
        }
      }
    }
  }
}
