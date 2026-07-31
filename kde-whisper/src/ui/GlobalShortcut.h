#pragma once

#include <QKeySequence>
#include <QList>
#include <QMetaType>
#include <QString>
#include <memory>

class QAction;
class QObject;

struct GlobalShortcutOwner
{
    QString componentName;
    QString actionId;

    bool operator==(const GlobalShortcutOwner &) const = default;
};

Q_DECLARE_METATYPE(GlobalShortcutOwner)

class IGlobalShortcutBackend
{
public:
    enum class Loading {
        PreserveExisting,
        UserOverride,
    };
    enum class ActionState {
        Missing,
        Existing,
        Unknown,
    };
    enum class MigrationState {
        NotStarted,
        Pending,
        Completed,
    };

    virtual ~IGlobalShortcutBackend() = default;

    virtual bool setDefaultShortcut(QAction *action, const QList<QKeySequence> &shortcuts) = 0;
    virtual bool setShortcut(QAction *action,
                             const QList<QKeySequence> &shortcuts,
                             Loading loading) = 0;
    virtual QList<QKeySequence> shortcut(const QAction *action) const = 0;
    virtual QList<QKeySequence> globalShortcut(const QString &componentName,
                                               const QString &actionId) const = 0;
    virtual ActionState shortcutActionState(const QString &componentName,
                                            const QString &actionId) const = 0;
    virtual QList<GlobalShortcutOwner> shortcutOwners(const QKeySequence &shortcut) const = 0;
    virtual void stealShortcutSystemwide(const QKeySequence &shortcut) = 0;
    virtual bool isShortcutAvailable(const QKeySequence &shortcut,
                                     const QString &componentName) const = 0;

    virtual MigrationState migrationState() const = 0;
    virtual QList<QKeySequence> pendingMigrationShortcuts() const = 0;
    virtual bool writePendingMigration(const QList<QKeySequence> &shortcuts) = 0;
    virtual bool markMigrationCompleted() = 0;
};

class GlobalShortcutManager
{
public:
    explicit GlobalShortcutManager(QObject *actionParent,
                                   std::unique_ptr<IGlobalShortcutBackend> backend = {});
    ~GlobalShortcutManager();

    QAction *action() const;
    QKeySequence currentShortcut() const;
    bool applyShortcut(const QKeySequence &shortcut, QString *error = nullptr);

    static QKeySequence defaultShortcut();
    static QString componentName();
    static QString actionId();
    static QString legacyComponentName();
    static QString legacyActionId();

private:
    enum class RegistrationResult {
        RegisterPreservingExisting,
        AlreadyRegistered,
        Deferred,
    };

    RegistrationResult migrateLegacyShortcut();
    bool completePendingMigration(const QList<QKeySequence> &shortcuts);
    void registerPreservingExisting();

    std::unique_ptr<IGlobalShortcutBackend> m_backend;
    QAction *m_action = nullptr;
};
