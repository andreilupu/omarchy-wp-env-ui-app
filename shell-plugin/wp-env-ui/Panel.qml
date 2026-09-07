import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// wp-env bar widget: a WordPress icon that opens a panel listing every
// wp-env project discovered by the local wp-env-ui server (favourites
// first), with start/stop/open controls per site. Right-click opens the
// full control-panel app window; the panel talks to the same HTTP API.
Panel {
  id: root
  moduleName: "wp-env-ui"
  ipcTarget: "wp-env-ui"

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property color favColor: "#d9a514"
  readonly property color okColor: "#5fb65f"

  readonly property int serverPort: setting("serverPort", 8710)
  readonly property int refreshIntervalSec: setting("refreshIntervalSec", 10)
  readonly property string apiBase: "http://127.0.0.1:" + serverPort

  property var sites: []
  property bool serverUp: false
  property bool serverStarting: false
  property bool cursorActive: false
  property int cursorIndex: 0
  property string lastError: ""
  property string dockerStatus: "ok"

  readonly property int runningCount: {
    var n = 0
    for (var i = 0; i < sites.length; i++)
      if (sites[i].state === "running") n++
    return n
  }
  readonly property bool transitioning: {
    for (var i = 0; i < sites.length; i++)
      if (sites[i].state === "starting" || sites[i].state === "stopping") return true
    return false
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

  function refresh() {
    if (!fetchProc.running) fetchProc.running = true
    if (opened && !pingProc.running) pingProc.running = true
  }

  // POST an action; the poll timer picks up the resulting state and a quick
  // refresh shortly after keeps the panel from feeling laggy. The response is
  // captured so API errors (409 already running, …) show up in the panel.
  function post(pathname, payload) {
    var cmd = [
      "curl", "-sS", "--max-time", "10", "-X", "POST",
      "-H", "Content-Type: application/json",
      "-d", JSON.stringify(payload),
      apiBase + pathname
    ]
    if (postProc.running) Quickshell.execDetached(cmd) // rare overlap: best effort
    else {
      postProc.command = cmd
      postProc.running = true
    }
    refreshSoon.restart()
  }

  function act(site, action) { post("/api/action", { path: site.path, action: action }) }
  function toggleFavorite(site) { post("/api/favorite", { path: site.path, favorite: !site.favorite }) }

  function primaryAction(site) {
    if (!site) return
    if (site.state === "running") act(site, "open")
    else if (site.state === "stopped") act(site, "start")
  }

  // Resolve wp-env-ui via PATH so every install layout works: install.sh
  // symlinks (~/.local/bin), the Arch package (/usr/bin), or mise (shims).
  function runLauncher(args) {
    Quickshell.execDetached(["bash", "-lc",
      "PATH=\"$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH\"; "
      + "exec wp-env-ui" + (args ? " " + args : "")])
  }

  function startServer() {
    serverStarting = true
    runLauncher("--server-only")
  }

  function openApp() {
    runLauncher("")
    root.close()
  }

  function dotColor(state) {
    if (state === "running") return okColor
    if (state === "starting" || state === "stopping") return favColor
    if (state === "error") return urgent
    return dim
  }

  onOpenedChanged: if (opened) {
    cursorActive = false
    cursorIndex = 0
    refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Process {
    id: fetchProc
    command: ["curl", "-fsS", "--max-time", "2", root.apiBase + "/api/sites"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var raw = String(text || "").trim()
        if (raw === "") {
          root.serverUp = false
          root.sites = []
          return
        }
        try {
          root.sites = JSON.parse(raw)
          root.serverUp = true
          root.serverStarting = false
        } catch (e) {
          root.serverUp = false
        }
      }
    }
  }

  Timer {
    interval: (root.opened || root.serverStarting || root.transitioning)
      ? 2000 : root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: refreshSoon
    interval: 500
    onTriggered: root.refresh()
  }

  Process {
    id: pingProc
    command: ["curl", "-fsS", "--max-time", "2", root.apiBase + "/api/ping"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var res = JSON.parse(String(text || ""))
          root.dockerStatus = res && res.docker ? res.docker : "ok"
        } catch (e) { /* server down: the serverUp path covers it */ }
      }
    }
  }

  Process {
    id: postProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = ""
        try {
          var res = JSON.parse(String(text || ""))
          if (res && res.error) message = res.error
        } catch (e) { /* empty/non-JSON response: nothing to show */ }
        root.lastError = message
        if (message !== "") errorClear.restart()
      }
    }
  }

  Timer {
    id: errorClear
    interval: 5000
    onTriggered: root.lastError = ""
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    foreground: root.runningCount > 0 ? root.foreground : root.dim
    tooltipText: root.serverUp
      ? root.runningCount + " of " + root.sites.length + " wp-env sites running"
      : "wp-env sites"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.openApp()
      else if (buttonCode === Qt.MiddleButton) root.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(520))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (dy !== 0 && root.sites.length > 0) {
          root.cursorActive = true
          root.cursorIndex = root.clamp(root.cursorIndex + dy, 0, root.sites.length - 1)
        }
      }
      onActivateRequested: {
        if (root.cursorActive && root.sites.length > 0)
          root.primaryAction(root.sites[root.cursorIndex])
        else if (!root.serverUp)
          root.startServer()
      }
      onDeleteRequested: {
        if (root.cursorActive && root.sites.length > 0) {
          var site = root.sites[root.cursorIndex]
          if (site.state === "running") root.act(site, "stop")
        }
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh()
        else if (t === "o" || t === "O") root.openApp()
        else if ((t === "f" || t === "F") && root.cursorActive && root.sites.length > 0)
          root.toggleFavorite(root.sites[root.cursorIndex])
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(8)

        RowLayout {
          width: parent.width
          spacing: Style.space(8)

          PanelSectionHeader {
            Layout.fillWidth: true
            text: "WP-ENV SITES"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }
          PanelActionButton {
            iconText: ""
            tooltipText: "Refresh (r)"
            foreground: root.foreground
            onClicked: root.refresh()
          }
          PanelActionButton {
            iconText: ""
            tooltipText: "Open control panel (o)"
            foreground: root.foreground
            onClicked: root.openApp()
          }
        }

        // ---------- docker down ----------
        Text {
          visible: root.serverUp && root.dockerStatus !== "ok"
          width: parent.width
          text: root.dockerStatus === "missing"
            ? "Docker is not installed — wp-env needs it to run sites."
            : "Docker is not running — sites can't start. systemctl start docker"
          wrapMode: Text.WordWrap
          color: root.favColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        // ---------- last action error ----------
        Text {
          visible: root.lastError !== ""
          width: parent.width
          text: root.lastError
          wrapMode: Text.WordWrap
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        // ---------- server down ----------
        Column {
          visible: !root.serverUp
          width: parent.width
          spacing: Style.space(10)

          Text {
            width: parent.width
            text: root.serverStarting
              ? "Starting the control panel server…"
              : "The control panel server is not running."
            wrapMode: Text.WordWrap
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
          Button {
            visible: !root.serverStarting
            text: "Start server"
            bordered: true
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.startServer()
          }
        }

        // ---------- empty ----------
        Text {
          visible: root.serverUp && root.sites.length === 0
          width: parent.width
          text: "No wp-env projects found."
          wrapMode: Text.WordWrap
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        // ---------- site rows ----------
        Repeater {
          model: root.serverUp ? root.sites : []

          delegate: Rectangle {
            id: row
            required property var modelData
            required property int index

            readonly property bool hasCursor: root.cursorActive && root.cursorIndex === index
            readonly property bool busy: modelData.state === "starting" || modelData.state === "stopping"

            width: column.width
            height: Style.space(38)
            radius: Style.space(8)
            color: rowArea.containsMouse || hasCursor ? root.hoverFill : "transparent"

            MouseArea {
              id: rowArea
              anchors.fill: parent
              hoverEnabled: true
              onClicked: root.primaryAction(row.modelData)
            }

            RowLayout {
              anchors.fill: parent
              anchors.leftMargin: Style.space(4)
              anchors.rightMargin: Style.space(6)
              spacing: Style.space(8)

              PanelActionButton {
                iconText: row.modelData.favorite ? "★" : "☆"
                tooltipText: row.modelData.favorite ? "Remove favourite (f)" : "Mark as favourite (f)"
                foreground: row.modelData.favorite ? root.favColor : root.dim
                hoverColor: root.favColor
                onClicked: root.toggleFavorite(row.modelData)
              }

              Rectangle {
                width: Style.space(8)
                height: Style.space(8)
                radius: width / 2
                color: root.dotColor(row.modelData.state)
              }

              Text {
                Layout.fillWidth: true
                text: row.modelData.name
                elide: Text.ElideMiddle
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }

              Text {
                text: ":" + row.modelData.port
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              PanelActionButton {
                visible: row.modelData.state === "running"
                iconText: ""
                tooltipText: "Open wp-admin"
                foreground: root.foreground
                onClicked: root.act(row.modelData, "open-admin")
              }
              PanelActionButton {
                visible: row.modelData.state === "running"
                iconText: ""
                tooltipText: "Open site"
                foreground: root.foreground
                onClicked: root.act(row.modelData, "open")
              }
              PanelActionButton {
                visible: !row.busy
                iconText: row.modelData.state === "running" ? "" : ""
                tooltipText: row.modelData.state === "running" ? "Stop (x)" : "Start"
                foreground: root.foreground
                hoverColor: row.modelData.state === "running" ? root.urgent : root.foreground
                onClicked: root.act(row.modelData, row.modelData.state === "running" ? "stop" : "start")
              }
              Text {
                visible: row.busy
                text: row.modelData.state === "starting" ? "starting…" : "stopping…"
                color: root.favColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }
      }
    }
  }
}
