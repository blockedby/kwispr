#pragma once

#include "ui/GlobalShortcut.h"

#include <QAction>
#include <utility>

class FakeGlobalShortcutBackend final : public IGlobalShortcutBackend
{
public:
    bool savedChoiceExists = false;
    QList<QKeySequence> savedChoice;
    QList<QKeySequence> currentChoice;
    QList<QKeySequence> defaultChoice;
    QKeySequence unavailableShortcut;
    QAction *registeredAction = nullptr;
    int defaultCalls = 0;
    int preserveCalls = 0;
    int userOverrideCalls = 0;

    bool setDefaultShortcut(QAction *action,
                            const QList<QKeySequence> &shortcuts) override
    {
        registeredAction = action;
        defaultChoice = shortcuts;
        ++defaultCalls;
        return true;
    }

    bool setShortcut(QAction *action,
                     const QList<QKeySequence> &shortcuts,
                     Loading loading) override
    {
        registeredAction = action;
        if (loading == Loading::PreserveExisting) {
            ++preserveCalls;
            if (savedChoiceExists) {
                currentChoice = savedChoice;
                return true;
            }
        } else {
            ++userOverrideCalls;
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

    bool isShortcutAvailable(const QKeySequence &shortcut,
                             const QString &) const override
    {
        return unavailableShortcut.isEmpty() || shortcut != unavailableShortcut;
    }
};
