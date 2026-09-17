// SPDX-License-Identifier: GPL-2.0-or-later
// Test-only fault: invoke stock EisDevice::setEnabled, without injecting input.
#include <plugin.h>
#include <input.h>
#include <core/inputdevice.h>

#include <QDBusConnection>
#include <QDBusContext>
#include <QDBusMessage>
#include <QDBusServiceWatcher>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPointer>
#include <QRegularExpression>
#include <QTimer>

#include <chrono>
#include <cstdio>
#include <sys/stat.h>
#include <unistd.h>

namespace {
constexpr auto controlPath = "/org/kde/KWin/Issue12EisFault";
QString generation;

qint64 monotonicNs()
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

bool ownedPath(const QString &path, bool socket = false)
{
    struct stat st {};
    if (::lstat(QFile::encodeName(path).constData(), &st) != 0 || st.st_uid != ::getuid()) {
        return false;
    }
    return socket ? (S_ISSOCK(st.st_mode) && (st.st_mode & 0077) == 0)
                  : (S_ISDIR(st.st_mode) && (st.st_mode & 0777) == 0700);
}

bool privateEnvironment()
{
    generation = qEnvironmentVariable("HARNESS_GENERATION");
    const auto runtime = qEnvironmentVariable("XDG_RUNTIME_DIR");
    if (qEnvironmentVariable("HARNESS_EIS_FAULT_PLUGIN") != "1"
        || !QRegularExpression("^[a-f0-9]{32}$").match(generation).hasMatch()
        || !QRegularExpression("^/tmp/kde-m1-[A-Za-z0-9_-]+$").match(runtime).hasMatch()
        || !ownedPath(runtime) || !ownedPath(runtime + "/home")
        || qEnvironmentVariable("HOME") != runtime + "/home"
        || qEnvironmentVariable("XDG_CONFIG_HOME") != runtime + "/home/config"
        || qEnvironmentVariable("DBUS_SESSION_BUS_ADDRESS") != "unix:path=" + runtime + "/bus"
        || !ownedPath(runtime + "/bus", true)) {
        return false;
    }
    QFile owner(runtime + "/owner.json");
    const QFileInfo info(owner);
    if (info.isSymLink() || info.ownerId() != ::getuid() || !owner.open(QIODevice::ReadOnly)) {
        return false;
    }
    return QJsonDocument::fromJson(owner.read(4096)).object().value("generation").toString() == generation;
}
}

class EisFault final : public KWin::Plugin, protected QDBusContext
{
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.kde.KWin.Issue12EisFault")

public:
    EisFault()
        : m_bus(QDBusConnection::sessionBus())
        , m_watcher(this)
    {
        m_timer.setSingleShot(true);
        m_timer.setTimerType(Qt::PreciseTimer);
        connect(&m_timer, &QTimer::timeout, this, [this] { resume("auto-resume"); });
        m_watcher.setConnection(m_bus);
        m_watcher.setWatchMode(QDBusServiceWatcher::WatchForUnregistration);
        connect(&m_watcher, &QDBusServiceWatcher::serviceUnregistered, this,
                [this](const QString &owner) { if (owner == m_owner) resume("controller-lost"); });
        m_registered = m_bus.isConnected() && m_bus.registerObject(
            controlPath, this, QDBusConnection::ExportAllSlots);
        record(m_registered ? "loaded" : "registration-failed", m_registered);
    }

    ~EisFault() override
    {
        resume("unload-resume");
        if (m_registered) m_bus.unregisterObject(controlPath);
        record("unloaded", true);
    }

    bool registered() const { return m_registered; }

public Q_SLOTS:
    QString Status(const QString &requestedGeneration)
    {
        if (!authorized(requestedGeneration)) return record("generation-or-context-rejected", false);
        return record("status", true);
    }

    QString Pause(const QString &requestedGeneration, const QString &clientName, uint durationMs)
    {
        if (!authorized(requestedGeneration)) return record("generation-or-context-rejected", false);
        if (!validClient(clientName) || durationMs < 1 || durationMs > 250)
            return record("selector-or-duration-rejected", false);
        if (!m_client.isEmpty()) return record("pause-already-active", false);
        auto *device = select(clientName);
        if (!device || !device->isEnabled()) return record("unique-enabled-keyboard-not-found", false);
        m_device = device;
        m_client = clientName;
        m_owner = message().service();
        m_watcher.addWatchedService(m_owner);
        m_timer.start(static_cast<int>(durationMs));
        const auto requestNs = monotonicNs();
        device->setEnabled(false);
        return record("paused", !device->isEnabled(), requestNs, durationMs);
    }

    QString Resume(const QString &requestedGeneration, const QString &clientName)
    {
        if (!authorized(requestedGeneration) || !validClient(clientName))
            return record("generation-or-context-rejected", false);
        if (clientName != m_client || message().service() != m_owner)
            return record("active-controller-or-selector-mismatch", false);
        return resume("explicit-resume");
    }

private:
    bool authorized(const QString &requestedGeneration) const
    {
        return m_registered && calledFromDBus() && requestedGeneration == generation
            && privateEnvironment();
    }

    bool validClient(const QString &clientName) const
    {
        return QRegularExpression("^issue12-" + generation + "-epoch-[1-9][0-9]{0,8}$")
            .match(clientName).hasMatch();
    }

    KWin::InputDevice *select(const QString &clientName) const
    {
        auto *input = KWin::input();
        if (!input) return nullptr;
        KWin::InputDevice *found = nullptr;
        for (auto *device : input->devices()) {
            if (QString::fromLatin1(device->metaObject()->className()) != "KWin::EisDevice"
                || device->name() != clientName + " eis keyboard" || !device->isKeyboard()) continue;
            if (found) return nullptr;
            found = device;
        }
        return found;
    }

    QString resume(const QString &reason)
    {
        m_timer.stop();
        if (m_client.isEmpty()) return {};
        const auto requestNs = monotonicNs();
        const bool matched = m_device && select(m_client) == m_device.data();
        if (matched) m_device->setEnabled(true);
        const auto result = record(reason, matched && m_device->isEnabled(), requestNs);
        m_watcher.removeWatchedService(m_owner);
        m_device.clear();
        m_owner.clear();
        m_client.clear();
        return result;
    }

    QString record(const QString &event, bool ok, qint64 requestNs = 0, uint durationMs = 0) const
    {
        QJsonObject data{{"source", "issue12_eis_fault"}, {"event", event}, {"ok", ok},
                         {"generation", generation}, {"client_name", m_client},
                         {"controller", m_owner}, {"kwin_abi", KWIN_PLUGIN_VERSION_STRING},
                         {"monotonic_ns", monotonicNs()}, {"request_monotonic_ns", requestNs},
                         {"duration_ms", static_cast<qint64>(durationMs)},
                         {"device_alive", !m_device.isNull()}};
        if (m_device) {
            data.insert("device_name", m_device->name());
            data.insert("device_class", QString::fromLatin1(m_device->metaObject()->className()));
            data.insert("enabled", m_device->isEnabled());
        }
        const auto json = QJsonDocument(data).toJson(QJsonDocument::Compact);
        std::fprintf(stderr, "%s\n", json.constData());
        return QString::fromUtf8(json);
    }

    QDBusConnection m_bus;
    QDBusServiceWatcher m_watcher;
    QTimer m_timer;
    QPointer<KWin::InputDevice> m_device;
    QString m_client;
    QString m_owner;
    bool m_registered = false;
};

class KWIN_EXPORT EisFaultFactory final : public KWin::PluginFactory
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID PluginFactory_iid FILE "metadata.json")
    Q_INTERFACES(KWin::PluginFactory)

public:
    std::unique_ptr<KWin::Plugin> create() const override
    {
        if (!privateEnvironment()) {
            std::fputs("issue12_eis_fault: private harness environment rejected\n", stderr);
            return nullptr;
        }
        auto plugin = std::make_unique<EisFault>();
        if (!plugin->registered()) return nullptr;
        return plugin;
    }
};

#include "main.moc"
