#include "ActivationRouting.h"
#include "AppMetadata.h"
#include "ui/TrayApp.h"

#include <KAboutData>
#include <KDBusService>

#include <QApplication>
#include <QDir>
#include <QFileInfo>
#include <QStandardPaths>
#include <QTimer>

namespace {
QString detectRuntimeRoot(const QString &executablePath)
{
    const QString configuredRoot = qEnvironmentVariable("KWISPR_RUNTIME_ROOT").trimmed();
    if (!configuredRoot.isEmpty()
        && QFileInfo::exists(QDir(configuredRoot).filePath(QStringLiteral("kwispr.sh")))
        && QFileInfo::exists(QDir(configuredRoot).filePath(QStringLiteral("models/local-stt-catalog.json")))) {
        return QDir(configuredRoot).canonicalPath();
    }

    QDir dir(QFileInfo(executablePath).absoluteDir());
    for (int i = 0; i < 5; ++i) {
        if (QFileInfo::exists(dir.filePath(QStringLiteral("kwispr.sh")))
            && QFileInfo::exists(dir.filePath(QStringLiteral("models/local-stt-catalog.json")))) {
            return dir.canonicalPath();
        }
        if (!dir.cdUp()) {
            break;
        }
    }
    return QDir::currentPath();
}
}

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    QApplication::setQuitOnLastWindowClosed(false);

    KAboutData aboutData(
        AppMetadata::appId(),
        AppMetadata::displayName(),
        AppMetadata::version(),
        QStringLiteral("KDE tray and settings shell for Kwispr"),
        KAboutLicense::GPL_V3);
    KAboutData::setApplicationData(aboutData);

    KDBusService dbusService(KDBusService::Unique);

    const QString runtimeRoot = detectRuntimeRoot(QCoreApplication::applicationFilePath());
    const QString cacheDir = QDir(QStandardPaths::writableLocation(QStandardPaths::GenericCacheLocation))
                                 .filePath(QStringLiteral("kwispr"));
    TrayApp tray(runtimeRoot, cacheDir);

    QObject::connect(&dbusService, &KDBusService::activateRequested, &tray,
                     [&tray](const QStringList &arguments, const QString &) {
        if (ActivationRouting::actionForArguments(arguments) == ActivationRouting::Action::OpenSettings) {
            tray.openSettings();
        }
    });

    if (ActivationRouting::actionForArguments(app.arguments()) == ActivationRouting::Action::OpenSettings) {
        QTimer::singleShot(0, &tray, [&tray]() {
            tray.openSettings();
            qApp->quit();
        });
    }

    return app.exec();
}
