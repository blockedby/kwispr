#pragma once

#include "ui/GlobalShortcut.h"

#include <QAction>
#include <QHash>
#include <utility>

class FakeGlobalShortcutBackend final : public IGlobalShortcutBackend
{
public:
    bool savedChoiceExists = false;
    QList<QKeySequence> savedChoice;
    QList<QKeySequence> currentChoice;
    QList<QKeySequence> defaultChoice;
    QList<QKeySequence> legacyChoice;
    QHash<QString, QList<GlobalShortcutOwner>> ownersByShortcut;
    QHash<QString, QList<QList<GlobalShortcutOwner>>> scriptedOwnerLookups;
    QKeySequence unavailableShortcut;
    QAction *registeredAction = nullptr;
    ActionState nativeActionState = ActionState::Missing;
    MigrationState storedMigrationState = MigrationState::NotStarted;
    QList<QKeySequence> pendingChoice;
    bool pendingWriteSucceeds = true;
    bool completionWriteSucceeds = true;
    int defaultCalls = 0;
    int preserveCalls = 0;
    int userOverrideCalls = 0;
    int ownerLookupCalls = 0;
    int stealCalls = 0;
    int pendingWriteCalls = 0;
    int completionCalls = 0;
    QStringList events;

    static QString key(const QKeySequence &shortcut)
    {
        return shortcut.toString(QKeySequence::PortableText);
    }

    void setOwners(const QKeySequence &shortcut,
                   const QList<GlobalShortcutOwner> &owners)
    {
        ownersByShortcut.insert(key(shortcut), owners);
    }

    bool setDefaultShortcut(QAction *action,
                            const QList<QKeySequence> &shortcuts) override
    {
        registeredAction = action;
        nativeActionState = ActionState::Existing;
        defaultChoice = shortcuts;
        ++defaultCalls;
        events.append(QStringLiteral("set-default"));
        return true;
    }

    bool setShortcut(QAction *action,
                     const QList<QKeySequence> &shortcuts,
                     Loading loading) override
    {
        registeredAction = action;
        nativeActionState = ActionState::Existing;
        if (loading == Loading::PreserveExisting) {
            ++preserveCalls;
            events.append(QStringLiteral("set-preserving"));
            if (savedChoiceExists) {
                currentChoice = savedChoice;
                return true;
            }
        } else {
            ++userOverrideCalls;
            events.append(QStringLiteral("set-no-autoloading"));
        }

        if (!shortcuts.isEmpty() && !isShortcutAvailable(shortcuts.constFirst(), QString())) {
            currentChoice.clear();
        } else {
            currentChoice = shortcuts;
        }
        savedChoiceExists = true;
        savedChoice = currentChoice;
        return true;
    }

    QList<QKeySequence> shortcut(const QAction *) const override
    {
        return currentChoice;
    }

    QList<QKeySequence> globalShortcut(const QString &componentName,
                                       const QString &actionId) const override
    {
        if (componentName == GlobalShortcutManager::legacyComponentName()
            && actionId == GlobalShortcutManager::legacyActionId()) {
            return legacyChoice;
        }
        if (componentName == GlobalShortcutManager::componentName()
            && actionId == GlobalShortcutManager::actionId()) {
            return savedChoiceExists ? savedChoice : currentChoice;
        }
        return {};
    }

    ActionState shortcutActionState(const QString &componentName,
                                    const QString &actionId) const override
    {
        if (componentName == GlobalShortcutManager::componentName()
            && actionId == GlobalShortcutManager::actionId()) {
            if (nativeActionState == ActionState::Unknown) {
                return ActionState::Unknown;
            }
            return savedChoiceExists ? ActionState::Existing : nativeActionState;
        }
        return ActionState::Missing;
    }

    QList<GlobalShortcutOwner> shortcutOwners(const QKeySequence &shortcut) const override
    {
        auto *that = const_cast<FakeGlobalShortcutBackend *>(this);
        ++that->ownerLookupCalls;
        const QString encoded = key(shortcut);
        that->events.append(QStringLiteral("inspect-owners:%1").arg(encoded));
        auto scripted = that->scriptedOwnerLookups.find(encoded);
        if (scripted != that->scriptedOwnerLookups.end() && !scripted->isEmpty()) {
            return scripted->takeFirst();
        }
        return ownersByShortcut.value(encoded);
    }

    void stealShortcutSystemwide(const QKeySequence &shortcut) override
    {
        ++stealCalls;
        events.append(QStringLiteral("steal:%1").arg(key(shortcut)));
        legacyChoice.removeAll(shortcut);
        ownersByShortcut.remove(key(shortcut));
    }

    bool isShortcutAvailable(const QKeySequence &shortcut,
                             const QString &) const override
    {
        if (!unavailableShortcut.isEmpty() && shortcut == unavailableShortcut) {
            return false;
        }
        const QList<GlobalShortcutOwner> owners = ownersByShortcut.value(key(shortcut));
        return owners.isEmpty();
    }

    MigrationState migrationState() const override
    {
        return storedMigrationState;
    }

    QList<QKeySequence> pendingMigrationShortcuts() const override
    {
        return pendingChoice;
    }

    bool writePendingMigration(const QList<QKeySequence> &shortcuts) override
    {
        ++pendingWriteCalls;
        events.append(QStringLiteral("write-pending"));
        if (!pendingWriteSucceeds) {
            return false;
        }
        storedMigrationState = MigrationState::Pending;
        pendingChoice = shortcuts;
        return true;
    }

    bool markMigrationCompleted() override
    {
        ++completionCalls;
        events.append(QStringLiteral("mark-completed"));
        if (!completionWriteSucceeds) {
            return false;
        }
        storedMigrationState = MigrationState::Completed;
        pendingChoice.clear();
        return true;
    }

    void changeExternally(const QKeySequence &shortcut)
    {
        currentChoice = shortcut.isEmpty() ? QList<QKeySequence>{}
                                           : QList<QKeySequence>{shortcut};
        savedChoiceExists = true;
        savedChoice = currentChoice;
    }
};
