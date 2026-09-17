// SPDX-License-Identifier: GPL-2.0-or-later
// Read embedded metadata only. Never instantiate or load the plugin.
#include <QCoreApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPluginLoader>
#include <cstdio>

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    if (argc != 2) return 2;
    QPluginLoader loader(QString::fromLocal8Bit(argv[1]));
    const auto metadata = loader.metaData();
    if (metadata.isEmpty()) return 1;
    const auto json = QJsonDocument(metadata).toJson(QJsonDocument::Compact);
    std::puts(json.constData());
    return 0;
}
