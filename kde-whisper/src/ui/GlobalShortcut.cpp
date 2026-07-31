#include "ui/GlobalShortcut.h"

#include "AppMetadata.h"

#include <KGlobalAccel>
#include <KGlobalShortcutInfo>

#include <QAction>
#include <QDBusArgument>
#include <QDBusInterface>
#include <QDBusMessage>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QSettings>
#include <QSet>
#include <utility>

namespace {
QString migrationStatePath()
{
    QString stateHome = qEnvironmentVariable("XDG_STATE_HOME").trimmed();
    if (stateHome.isEmpty()) {
        stateHome = QDir(QDir::homePath()).filePath(QStringLiteral(".local/state"));
    }
    return QDir(stateHome).filePath(QStringLiteral("kwispr/global-shortcut-migration.ini"));
}

class MigrationJournal
{
public:
    MigrationJournal()
        : m_path(migrationStatePath())
    {
    }

    IGlobalShortcutBackend::MigrationState state() const
    {
        QSettings settings(m_path, QSettings::IniFormat);
        const QString status = settings.value(QStringLiteral("migration/status")).toString();
        if (status == QStringLiteral("pending")) {
            return IGlobalShortcutBackend::MigrationState::Pending;
        }
        if (status == QStringLiteral("completed")) {
            return IGlobalShortcutBackend::MigrationState::Completed;
        }
        return IGlobalShortcutBackend::MigrationState::NotStarted;
    }

    QList<QKeySequence> pendingShortcuts() const
    {
        QSettings settings(m_path, QSettings::IniFormat);
        const QStringList encoded = settings.value(QStringLiteral("migration/shortcuts")).toStringList();
        QList<QKeySequence> shortcuts;
        shortcuts.reserve(encoded.size());
        for (const QString &value : encoded) {
            const QKeySequence shortcut = QKeySequence::fromString(value, QKeySequence::PortableText);
            if (shortcut.isEmpty()) {
                return {};
            }
            shortcuts.append(shortcut);
        }
        return shortcuts;
    }

    bool writePending(const QList<QKeySequence> &shortcuts)
    {
        if (shortcuts.isEmpty() || !QDir().mkpath(QFileInfo(m_path).absolutePath())) {
            return false;
        }
        QStringList encoded;
        encoded.reserve(shortcuts.size());
        for (const QKeySequence &shortcut : shortcuts) {
            if (shortcut.isEmpty()) {
                return false;
            }
            encoded.append(shortcut.toString(QKeySequence::PortableText));
        }

        QSettings settings(m_path, QSettings::IniFormat);
        settings.setAtomicSyncRequired(true);
        settings.setValue(QStringLiteral("migration/version"), 1);
        settings.setValue(QStringLiteral("migration/status"), QStringLiteral("pending"));
        settings.setValue(QStringLiteral("migration/shortcuts"), encoded);
        settings.sync();
        if (settings.status() != QSettings::NoError) {
            return false;
        }
        QFile::setPermissions(m_path, QFileDevice::ReadOwner | QFileDevice::WriteOwner);
        return true;
    }

    bool markCompleted()
    {
        if (!QDir().mkpath(QFileInfo(m_path).absolutePath())) {
            return false;
        }
        QSettings settings(m_path, QSettings::IniFormat);
        settings.setAtomicSyncRequired(true);
        settings.setValue(QStringLiteral("migration/version"), 1);
        settings.setValue(QStringLiteral("migration/status"), QStringLiteral("completed"));
        settings.remove(QStringLiteral("migration/shortcuts"));
        settings.sync();
        if (settings.status() != QSettings::NoError) {
            return false;
        }
        QFile::setPermissions(m_path, QFileDevice::ReadOwner | QFileDevice::WriteOwner);
        return true;
    }

private:
    QString m_path;
};

class KGlobalAccelBackend final : public IGlobalShortcutBackend
{
public:
    bool setDefaultShortcut(QAction *action, const QList<QKeySequence> &shortcuts) override
    {
        return KGlobalAccel::self()->setDefaultShortcut(action, shortcuts);
    }

    bool setShortcut(QAction *action,
                     const QList<QKeySequence> &shortcuts,
                     Loading loading) override
    {
        const auto loadFlag = loading == Loading::PreserveExisting
            ? KGlobalAccel::Autoloading
            : KGlobalAccel::NoAutoloading;
        return KGlobalAccel::self()->setShortcut(action, shortcuts, loadFlag);
    }

    QList<QKeySequence> shortcut(const QAction *action) const override
    {
        return KGlobalAccel::self()->shortcut(action);
    }

    QList<QKeySequence> globalShortcut(const QString &componentName,
                                       const QString &actionId) const override
    {
        return KGlobalAccel::self()->globalShortcut(componentName, actionId);
    }

    ActionState shortcutActionState(const QString &componentName,
                                    const QString &actionId) const override
    {
        QDBusInterface interface(QStringLiteral("org.kde.kglobalaccel"),
                                 QStringLiteral("/kglobalaccel"),
                                 QStringLiteral("org.kde.KGlobalAccel"));
        const QStringList componentId{componentName, QString(), QString(), QString()};
        const QDBusMessage reply = interface.call(QStringLiteral("allActionsForComponent"),
                                                  componentId);
        if (reply.type() == QDBusMessage::ErrorMessage || reply.arguments().isEmpty()) {
            return ActionState::Unknown;
        }

        const QList<QStringList> actions = qdbus_cast<QList<QStringList>>(reply.arguments().constFirst());
        for (const QStringList &candidate : actions) {
            if (candidate.size() >= 2 && candidate.at(0) == componentName
                && candidate.at(1) == actionId) {
                return ActionState::Existing;
            }
        }
        return ActionState::Missing;
    }

    QList<GlobalShortcutOwner> shortcutOwners(const QKeySequence &shortcut) const override
    {
        QList<GlobalShortcutOwner> owners;
        const QList<KGlobalShortcutInfo> registrations =
            KGlobalAccel::globalShortcutsByKey(shortcut, KGlobalAccel::Equal);
        owners.reserve(registrations.size());
        for (const KGlobalShortcutInfo &registration : registrations) {
            owners.append({registration.componentUniqueName(), registration.uniqueName()});
        }
        return owners;
    }

    void stealShortcutSystemwide(const QKeySequence &shortcut) override
    {
        KGlobalAccel::stealShortcutSystemwide(shortcut);
    }

    bool isShortcutAvailable(const QKeySequence &shortcut,
                             const QString &componentName) const override
    {
        return KGlobalAccel::isGlobalShortcutAvailable(shortcut, componentName);
    }

    MigrationState migrationState() const override
    {
        return m_journal.state();
    }

    QList<QKeySequence> pendingMigrationShortcuts() const override
    {
        return m_journal.pendingShortcuts();
    }

    bool writePendingMigration(const QList<QKeySequence> &shortcuts) override
    {
        return m_journal.writePending(shortcuts);
    }

    bool markMigrationCompleted() override
    {
        return m_journal.markCompleted();
    }

private:
    MigrationJournal m_journal;
};

QList<QKeySequence> shortcutList(const QKeySequence &shortcut)
{
    return shortcut.isEmpty() ? QList<QKeySequence>{}
                              : QList<QKeySequence>{shortcut};
}

bool ownersAreExactly(const QList<GlobalShortcutOwner> &owners,
                      const GlobalShortcutOwner &expected)
{
    if (owners.isEmpty()) {
        return false;
    }
    for (const GlobalShortcutOwner &owner : owners) {
        if (owner != expected) {
            return false;
        }
    }
    return true;
}

bool shortcutsAreExactly(const QList<QKeySequence> &actual,
                         const QList<QKeySequence> &expected)
{
    if (actual.size() != expected.size()) {
        return false;
    }
    QSet<QKeySequence> actualSet;
    QSet<QKeySequence> expectedSet;
    for (const QKeySequence &shortcut : actual) {
        actualSet.insert(shortcut);
    }
    for (const QKeySequence &shortcut : expected) {
        expectedSet.insert(shortcut);
    }
    return actualSet.size() == actual.size() && actualSet == expectedSet;
}
}

GlobalShortcutManager::GlobalShortcutManager(
    QObject *actionParent,
    std::unique_ptr<IGlobalShortcutBackend> backend)
    : m_backend(backend ? std::move(backend) : std::make_unique<KGlobalAccelBackend>())
    , m_action(new QAction(actionParent))
{
    m_action->setObjectName(actionId());
    m_action->setText(QStringLiteral("Toggle Dictation Recording"));
    m_action->setProperty("componentName", componentName());
    m_action->setProperty("componentDisplayName", AppMetadata::displayName());
    m_action->setAutoRepeat(false);

    const RegistrationResult result = migrateLegacyShortcut();
    if (result == RegistrationResult::RegisterPreservingExisting) {
        registerPreservingExisting();
    }
}

GlobalShortcutManager::~GlobalShortcutManager() = default;

QAction *GlobalShortcutManager::action() const
{
    return m_action;
}

QKeySequence GlobalShortcutManager::currentShortcut() const
{
    const QList<QKeySequence> shortcuts = m_backend->shortcut(m_action);
    return shortcuts.isEmpty() ? QKeySequence() : shortcuts.constFirst();
}

bool GlobalShortcutManager::applyShortcut(const QKeySequence &shortcut, QString *error)
{
    if (error) {
        error->clear();
    }

    const QKeySequence previous = currentShortcut();
    if (shortcut == previous) {
        return true;
    }
    if (shortcut.count() > 1) {
        if (error) {
            *error = QStringLiteral("Use a single key combination for the global dictation shortcut.");
        }
        return false;
    }
    if (!shortcut.isEmpty()
        && !m_backend->isShortcutAvailable(shortcut, componentName())) {
        if (error) {
            *error = QStringLiteral("The shortcut %1 is already assigned to another KDE global action. Choose a different shortcut; nothing was changed.")
                         .arg(shortcut.toString(QKeySequence::NativeText));
        }
        return false;
    }

    const QList<QKeySequence> requested = shortcutList(shortcut);
    const bool set = m_backend->setShortcut(
        m_action, requested, IGlobalShortcutBackend::Loading::UserOverride);
    if (set && currentShortcut() == shortcut) {
        return true;
    }

    // A conflict can appear between the availability check and registration.
    // Restore the prior value rather than leaving a partially changed action.
    const bool restored = m_backend->setShortcut(
        m_action, shortcutList(previous),
        IGlobalShortcutBackend::Loading::UserOverride)
        && currentShortcut() == previous;
    if (error) {
        const QString requestedText = shortcut.isEmpty()
            ? QStringLiteral("the cleared shortcut")
            : shortcut.toString(QKeySequence::NativeText);
        *error = restored
            ? QStringLiteral("KDE could not register %1. The previous global shortcut was kept.")
                  .arg(requestedText)
            : QStringLiteral("KDE could not register %1 or restore the previous shortcut. Review it in KDE Global Shortcuts.")
                  .arg(requestedText);
    }
    return false;
}

GlobalShortcutManager::RegistrationResult GlobalShortcutManager::migrateLegacyShortcut()
{
    using MigrationState = IGlobalShortcutBackend::MigrationState;
    using ActionState = IGlobalShortcutBackend::ActionState;

    const MigrationState migration = m_backend->migrationState();
    if (migration == MigrationState::Completed) {
        return RegistrationResult::RegisterPreservingExisting;
    }

    if (migration == MigrationState::Pending) {
        const QList<QKeySequence> pending = m_backend->pendingMigrationShortcuts();
        if (pending.isEmpty()) {
            // A pending journal without valid keys is not safe to discard or
            // reinterpret. Leave it untouched for manual diagnosis.
            return RegistrationResult::Deferred;
        }

        const ActionState nativeState =
            m_backend->shortcutActionState(componentName(), actionId());
        if (nativeState == ActionState::Unknown) {
            return RegistrationResult::Deferred;
        }
        if (nativeState == ActionState::Existing) {
            // This is either the migration registration from before an interruption,
            // or a user choice made while recovery was pending. In both cases the
            // native action wins; never steal again.
            m_backend->markMigrationCompleted();
            return RegistrationResult::RegisterPreservingExisting;
        }

        const GlobalShortcutOwner legacy{legacyComponentName(), legacyActionId()};
        for (const QKeySequence &shortcut : pending) {
            const QList<GlobalShortcutOwner> owners = m_backend->shortcutOwners(shortcut);
            if (!owners.isEmpty() && !ownersAreExactly(owners, legacy)) {
                // Ownership changed while an interruption was pending. Refuse
                // the remainder permanently rather than retrying a one-way move.
                m_backend->markMigrationCompleted();
                return RegistrationResult::RegisterPreservingExisting;
            }
        }
        if (completePendingMigration(pending)) {
            return RegistrationResult::AlreadyRegistered;
        }
        return m_backend->migrationState() == MigrationState::Completed
            ? RegistrationResult::RegisterPreservingExisting
            : RegistrationResult::Deferred;
    }

    const ActionState nativeState =
        m_backend->shortcutActionState(componentName(), actionId());
    if (nativeState == ActionState::Unknown) {
        // Failure to prove that no native custom/cleared action exists must not
        // be turned into a fresh registration.
        return RegistrationResult::Deferred;
    }
    if (nativeState == ActionState::Existing) {
        // This includes an explicitly-cleared native action, which is not
        // distinguishable by its empty key list alone.
        m_backend->markMigrationCompleted();
        return RegistrationResult::RegisterPreservingExisting;
    }

    const QList<QKeySequence> legacyShortcuts =
        m_backend->globalShortcut(legacyComponentName(), legacyActionId());
    if (legacyShortcuts.isEmpty()) {
        m_backend->markMigrationCompleted();
        return RegistrationResult::RegisterPreservingExisting;
    }

    const GlobalShortcutOwner legacy{legacyComponentName(), legacyActionId()};
    for (const QKeySequence &shortcut : legacyShortcuts) {
        if (!ownersAreExactly(m_backend->shortcutOwners(shortcut), legacy)) {
            // A mixed, foreign, or stale owner is never touched. Recording the
            // decision prevents a later startup from retrying a one-way move.
            m_backend->markMigrationCompleted();
            return RegistrationResult::RegisterPreservingExisting;
        }
    }

    // This durable journal must exist before the first irreversible steal.
    if (!m_backend->writePendingMigration(legacyShortcuts)) {
        return RegistrationResult::RegisterPreservingExisting;
    }
    if (completePendingMigration(legacyShortcuts)) {
        return RegistrationResult::AlreadyRegistered;
    }
    return m_backend->migrationState() == MigrationState::Completed
        ? RegistrationResult::RegisterPreservingExisting
        : RegistrationResult::Deferred;
}

bool GlobalShortcutManager::completePendingMigration(const QList<QKeySequence> &shortcuts)
{
    const GlobalShortcutOwner legacy{legacyComponentName(), legacyActionId()};
    QList<QKeySequence> stillLegacyOwned;
    // Revalidate the complete set before the first irreversible operation. A
    // later bad key must never leave an earlier key partially migrated.
    for (const QKeySequence &shortcut : shortcuts) {
        const QList<GlobalShortcutOwner> owners = m_backend->shortcutOwners(shortcut);
        if (!owners.isEmpty()) {
            if (!ownersAreExactly(owners, legacy)) {
                m_backend->markMigrationCompleted();
                return false;
            }
            stillLegacyOwned.append(shortcut);
        }
    }
    for (const QKeySequence &shortcut : stillLegacyOwned) {
        m_backend->stealShortcutSystemwide(shortcut);
    }

    m_backend->setDefaultShortcut(m_action, {defaultShortcut()});
    const bool registered = m_backend->setShortcut(
        m_action, shortcuts, IGlobalShortcutBackend::Loading::UserOverride);
    const bool nativeExact = registered
        && shortcutsAreExactly(m_backend->shortcut(m_action), shortcuts)
        && shortcutsAreExactly(
            m_backend->globalShortcut(componentName(), actionId()), shortcuts);
    const bool legacyEmpty =
        m_backend->globalShortcut(legacyComponentName(), legacyActionId()).isEmpty();
    if (!nativeExact || !legacyEmpty) {
        return false;
    }
    return m_backend->markMigrationCompleted();
}

void GlobalShortcutManager::registerPreservingExisting()
{
    const QList<QKeySequence> defaults{defaultShortcut()};
    m_backend->setDefaultShortcut(m_action, defaults);
    // Autoloading restores a saved custom or explicitly-cleared native value,
    // and only uses Ctrl+. for a fresh registration.
    m_backend->setShortcut(m_action, defaults,
                           IGlobalShortcutBackend::Loading::PreserveExisting);
}

QKeySequence GlobalShortcutManager::defaultShortcut()
{
    return QKeySequence(Qt::CTRL | Qt::Key_Period);
}

QString GlobalShortcutManager::componentName()
{
    return AppMetadata::appId();
}

QString GlobalShortcutManager::actionId()
{
    return QStringLiteral("toggle-dictation-recording");
}

QString GlobalShortcutManager::legacyComponentName()
{
    return QStringLiteral("org.kwispr.KdeWhisper.desktop");
}

QString GlobalShortcutManager::legacyActionId()
{
    return QStringLiteral("_launch");
}
