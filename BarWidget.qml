import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Bar icon: a click opens the video wallpaper search.
BarWidget {
  id: root
  moduleName: "tinyworkshop.livewall"

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰕧"
    tooltipText: strings.t("bar.tooltip")
    fixedWidth: root.bar && root.bar.vertical ? -1 : Style.space(27)
    fixedHeight: root.bar && root.bar.vertical ? Style.space(26) : -1
    onPressed: function(b) {
      if (root.bar) root.bar.run("omarchy-shell shell toggle tinyworkshop.livewall")
    }
  }
}
