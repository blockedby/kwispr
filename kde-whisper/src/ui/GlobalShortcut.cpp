#include "ui/GlobalShortcut.h"

#include "AppMetadata.h"

#include <KGlobalAccel>

#include <QAction>
#include <utility>

namespace {
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

    bool isShortcutAvailable(const QKeySequence &shortcut,
                             const QString &componentName) const override
    {
        return KGlobalAccel::isGlobalShortcutAvailable(shortcut, componentName);
    }
};

QList<QKeySequence> shortcutList(const QKeySequence &shortcut)
{
    return shortcut.isEmpty() ? QList<QKeySequence>{}
                              : QList<QKeySequence>{shortcut};
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

    const QList<QKeySequence> defaults{defaultShortcut()};
    m_backend->setDefaultShortcut(m_action, defaults);
    // Autoloading is essential here: KGlobalAccel restores a saved custom or
    // explicitly cleared value, and only uses Ctrl+. for a fresh registration.
    m_backend->setShortcut(m_action, defaults,
                           IGlobalShortcutBackend::Loading::PreserveExisting);
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
    return applyShortcutIfCurrent(shortcut, currentShortcut(), error);
}

bool GlobalShortcutManager::applyShortcutIfCurrent(
    const QKeySequence &shortcut,
    const QKeySequence &expectedCurrent,
    QString *error)
{
    if (error) {
        error->clear();
    }

    if (shortcut.count() > 1) {
        if (error) {
            *error = QStringLiteral("Use a single key combination for the global dictation shortcut.");
        }
        return false;
    }
    const bool available = shortcut.isEmpty()
        || m_backend->isShortcutAvailable(shortcut, componentName());
    const QList<QKeySequence> requested = shortcutList(shortcut);

    // KGlobalAccel has no conditional set API. Keep this authoritative re-read
    // as the final backend operation before the NoAutoloading mutation.
    const QKeySequence previous = currentShortcut();
    if (previous != expectedCurrent) {
        if (error) {
            const QString currentText = previous.isEmpty()
                ? QStringLiteral("disabled")
                : previous.toString(QKeySequence::NativeText);
            *error = QStringLiteral(
                "The global shortcut changed in KDE while Settings was open (it is now %1). "
                "Your shortcut edit was not applied, and nothing was changed. Close and reopen Settings to review the current shortcut before trying again.")
                         .arg(currentText);
        }
        return false;
    }
    if (!available) {
        if (error) {
            *error = QStringLiteral("The shortcut %1 is already assigned to another KDE global action. Choose a different shortcut; nothing was changed.")
                         .arg(shortcut.toString(QKeySequence::NativeText));
        }
        return false;
    }
    if (shortcut == previous) {
        return true;
    }

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
