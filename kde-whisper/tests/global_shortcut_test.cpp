#include "fake_global_shortcut.h"
#include "ui/GlobalShortcut.h"

#include <QAction>
#include <QObject>
#include <QtTest/QtTest>
#include <memory>

namespace {
GlobalShortcutOwner legacyOwner()
{
    return {GlobalShortcutManager::legacyComponentName(),
            GlobalShortcutManager::legacyActionId()};
}

GlobalShortcutOwner foreignOwner()
{
    return {QStringLiteral("org.example.Foreign"), QStringLiteral("foreign-action")};
}
}

class GlobalShortcutTest : public QObject
{
    Q_OBJECT
private slots:
    void freshRegistrationUsesDefaultOnlyWhenAvailable();
    void savedCustomAndClearedChoicesArePreserved();
    void customAndClearedChoicesPersistImmediately();
    void conflictFailsWithoutMutation();
    void migratesExactLegacyCtrlPeriod();
    void migratesExactLegacyCustomShortcut();
    void refusesForeignAndMixedLegacyOwnership_data();
    void refusesForeignAndMixedLegacyOwnership();
    void recoversPendingMigrationAfterLegacyWasStolen();
    void refusesAllKeysBeforeAnySteal();
    void unknownNativeStateDefersWithoutMutation();
    void malformedPendingJournalDefersWithoutMutation();
    void completedMigrationIsNeverRepeated();
};

void GlobalShortcutTest::freshRegistrationUsesDefaultOnlyWhenAvailable()
{
    QObject owner;
    auto availableBackend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *available = availableBackend.get();
    GlobalShortcutManager availableManager(&owner, std::move(availableBackend));

    QCOMPARE(available->defaultCalls, 1);
    QCOMPARE(available->preserveCalls, 1);
    QCOMPARE(available->defaultChoice,
             QList<QKeySequence>{GlobalShortcutManager::defaultShortcut()});
    QCOMPARE(availableManager.currentShortcut(), QKeySequence(QStringLiteral("Ctrl+.")));
    QCOMPARE(available->registeredAction->objectName(), GlobalShortcutManager::actionId());
    QCOMPARE(available->registeredAction->property("componentName").toString(),
             GlobalShortcutManager::componentName());

    QObject conflictedOwner;
    auto conflictedBackend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *conflicted = conflictedBackend.get();
    conflicted->unavailableShortcut = GlobalShortcutManager::defaultShortcut();
    GlobalShortcutManager conflictedManager(&conflictedOwner, std::move(conflictedBackend));

    QVERIFY(conflictedManager.currentShortcut().isEmpty());
    QVERIFY(conflicted->savedChoiceExists);
    QVERIFY(conflicted->savedChoice.isEmpty());
}

void GlobalShortcutTest::savedCustomAndClearedChoicesArePreserved()
{
    const QKeySequence legacy(QStringLiteral("Ctrl+."));
    const QKeySequence custom(QStringLiteral("Meta+Alt+D"));
    QObject customOwner;
    auto customBackend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *customFake = customBackend.get();
    customFake->savedChoiceExists = true;
    customFake->savedChoice = {custom};
    customFake->legacyChoice = {legacy};
    customFake->setOwners(legacy, {legacyOwner()});
    GlobalShortcutManager customManager(&customOwner, std::move(customBackend));
    QCOMPARE(customManager.currentShortcut(), custom);
    QCOMPARE(customFake->userOverrideCalls, 0);
    QCOMPARE(customFake->stealCalls, 0);
    QCOMPARE(customFake->legacyChoice, QList<QKeySequence>{legacy});

    QObject clearedOwner;
    auto clearedBackend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *clearedFake = clearedBackend.get();
    clearedFake->savedChoiceExists = true;
    clearedFake->legacyChoice = {legacy};
    clearedFake->setOwners(legacy, {legacyOwner()});
    GlobalShortcutManager clearedManager(&clearedOwner, std::move(clearedBackend));
    QVERIFY(clearedManager.currentShortcut().isEmpty());
    QCOMPARE(clearedFake->userOverrideCalls, 0);
    QCOMPARE(clearedFake->stealCalls, 0);
    QCOMPARE(clearedFake->legacyChoice, QList<QKeySequence>{legacy});
}

void GlobalShortcutTest::customAndClearedChoicesPersistImmediately()
{
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    GlobalShortcutManager manager(&owner, std::move(backend));
    const QKeySequence custom(QStringLiteral("Ctrl+Alt+D"));

    QString error;
    QVERIFY(manager.applyShortcut(custom, &error));
    QVERIFY(error.isEmpty());
    QCOMPARE(manager.currentShortcut(), custom);
    QCOMPARE(fake->savedChoice, QList<QKeySequence>{custom});
    QCOMPARE(fake->userOverrideCalls, 1);

    QVERIFY(manager.applyShortcut(QKeySequence(), &error));
    QVERIFY(error.isEmpty());
    QVERIFY(manager.currentShortcut().isEmpty());
    QVERIFY(fake->savedChoiceExists);
    QVERIFY(fake->savedChoice.isEmpty());
    QCOMPARE(fake->userOverrideCalls, 2);
}

void GlobalShortcutTest::conflictFailsWithoutMutation()
{
    const QKeySequence saved(QStringLiteral("Meta+D"));
    const QKeySequence conflict(QStringLiteral("Ctrl+Alt+D"));
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->savedChoiceExists = true;
    fake->savedChoice = {saved};
    fake->unavailableShortcut = conflict;
    GlobalShortcutManager manager(&owner, std::move(backend));

    QString error;
    QVERIFY(!manager.applyShortcut(conflict, &error));
    QVERIFY(error.contains(QStringLiteral("already assigned")));
    QVERIFY(error.contains(QStringLiteral("nothing was changed")));
    QCOMPARE(manager.currentShortcut(), saved);
    QCOMPARE(fake->savedChoice, QList<QKeySequence>{saved});
    QCOMPARE(fake->userOverrideCalls, 0);
}

void GlobalShortcutTest::migratesExactLegacyCtrlPeriod()
{
    const QKeySequence legacy(QStringLiteral("Ctrl+."));
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->legacyChoice = {legacy};
    fake->setOwners(legacy, {legacyOwner()});

    GlobalShortcutManager manager(&owner, std::move(backend));

    QCOMPARE(manager.currentShortcut(), legacy);
    QVERIFY(fake->legacyChoice.isEmpty());
    QCOMPARE(fake->savedChoice, QList<QKeySequence>{legacy});
    QCOMPARE(fake->pendingWriteCalls, 1);
    QCOMPARE(fake->stealCalls, 1);
    QCOMPARE(fake->userOverrideCalls, 1);
    QCOMPARE(fake->preserveCalls, 0);
    QCOMPARE(fake->storedMigrationState, IGlobalShortcutBackend::MigrationState::Completed);
    QVERIFY(fake->pendingChoice.isEmpty());
    QVERIFY(fake->events.indexOf(QStringLiteral("write-pending"))
            < fake->events.indexOf(QStringLiteral("steal:Ctrl+.")));
    QVERIFY(fake->events.indexOf(QStringLiteral("steal:Ctrl+."))
            < fake->events.indexOf(QStringLiteral("set-no-autoloading")));
    QVERIFY(fake->events.indexOf(QStringLiteral("set-no-autoloading"))
            < fake->events.indexOf(QStringLiteral("mark-completed")));
}

void GlobalShortcutTest::migratesExactLegacyCustomShortcut()
{
    const QKeySequence legacy(QStringLiteral("Meta+Alt+D"));
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->legacyChoice = {legacy};
    fake->setOwners(legacy, {legacyOwner(), legacyOwner()});

    GlobalShortcutManager manager(&owner, std::move(backend));

    QCOMPARE(manager.currentShortcut(), legacy);
    QCOMPARE(fake->savedChoice, QList<QKeySequence>{legacy});
    QVERIFY(fake->legacyChoice.isEmpty());
    QCOMPARE(fake->stealCalls, 1);
    QCOMPARE(fake->userOverrideCalls, 1);
}

void GlobalShortcutTest::refusesForeignAndMixedLegacyOwnership_data()
{
    QTest::addColumn<QList<GlobalShortcutOwner>>("owners");
    QTest::newRow("foreign") << QList<GlobalShortcutOwner>{foreignOwner()};
    QTest::newRow("mixed") << QList<GlobalShortcutOwner>{legacyOwner(), foreignOwner()};
}

void GlobalShortcutTest::refusesForeignAndMixedLegacyOwnership()
{
    QFETCH(QList<GlobalShortcutOwner>, owners);
    const QKeySequence legacy(QStringLiteral("Meta+Alt+D"));
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->legacyChoice = {legacy};
    fake->setOwners(legacy, owners);

    GlobalShortcutManager manager(&owner, std::move(backend));

    QCOMPARE(fake->legacyChoice, QList<QKeySequence>{legacy});
    QCOMPARE(fake->stealCalls, 0);
    QCOMPARE(fake->pendingWriteCalls, 0);
    QCOMPARE(fake->userOverrideCalls, 0);
    QCOMPARE(fake->preserveCalls, 1);
    QCOMPARE(fake->storedMigrationState, IGlobalShortcutBackend::MigrationState::Completed);
}

void GlobalShortcutTest::recoversPendingMigrationAfterLegacyWasStolen()
{
    const QKeySequence legacy(QStringLiteral("Ctrl+Alt+D"));
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->storedMigrationState = IGlobalShortcutBackend::MigrationState::Pending;
    fake->pendingChoice = {legacy};
    // Empty owners and legacy keys model interruption after steal and before
    // native action registration.

    GlobalShortcutManager manager(&owner, std::move(backend));

    QCOMPARE(manager.currentShortcut(), legacy);
    QCOMPARE(fake->savedChoice, QList<QKeySequence>{legacy});
    QCOMPARE(fake->stealCalls, 0);
    QCOMPARE(fake->userOverrideCalls, 1);
    QCOMPARE(fake->storedMigrationState, IGlobalShortcutBackend::MigrationState::Completed);
    QVERIFY(fake->pendingChoice.isEmpty());
}

void GlobalShortcutTest::refusesAllKeysBeforeAnySteal()
{
    const QKeySequence first(QStringLiteral("Ctrl+."));
    const QKeySequence second(QStringLiteral("Meta+Alt+D"));
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->legacyChoice = {first, second};
    fake->setOwners(first, {legacyOwner()});
    fake->setOwners(second, {legacyOwner()});
    // Both keys pass initial validation. During the full pre-steal recheck,
    // ownership of the second key has changed to a foreign action.
    fake->scriptedOwnerLookups.insert(FakeGlobalShortcutBackend::key(first),
                                      {{legacyOwner()}, {legacyOwner()}});
    fake->scriptedOwnerLookups.insert(FakeGlobalShortcutBackend::key(second),
                                      {{legacyOwner()}, {foreignOwner()}});

    GlobalShortcutManager manager(&owner, std::move(backend));

    QCOMPARE(fake->stealCalls, 0);
    QCOMPARE(fake->legacyChoice, (QList<QKeySequence>{first, second}));
    QCOMPARE(fake->storedMigrationState, IGlobalShortcutBackend::MigrationState::Completed);
    QCOMPARE(fake->userOverrideCalls, 0);
    QCOMPARE(fake->preserveCalls, 1);
}

void GlobalShortcutTest::unknownNativeStateDefersWithoutMutation()
{
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->nativeActionState = IGlobalShortcutBackend::ActionState::Unknown;

    GlobalShortcutManager manager(&owner, std::move(backend));

    QCOMPARE(fake->defaultCalls, 0);
    QCOMPARE(fake->preserveCalls, 0);
    QCOMPARE(fake->userOverrideCalls, 0);
    QCOMPARE(fake->stealCalls, 0);
    QVERIFY(manager.currentShortcut().isEmpty());
}

void GlobalShortcutTest::malformedPendingJournalDefersWithoutMutation()
{
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->storedMigrationState = IGlobalShortcutBackend::MigrationState::Pending;

    GlobalShortcutManager manager(&owner, std::move(backend));

    QCOMPARE(fake->completionCalls, 0);
    QCOMPARE(fake->defaultCalls, 0);
    QCOMPARE(fake->preserveCalls, 0);
    QCOMPARE(fake->userOverrideCalls, 0);
    QCOMPARE(fake->stealCalls, 0);
    QCOMPARE(fake->storedMigrationState, IGlobalShortcutBackend::MigrationState::Pending);
}

void GlobalShortcutTest::completedMigrationIsNeverRepeated()
{
    const QKeySequence legacy(QStringLiteral("Ctrl+."));
    QObject owner;
    auto backend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *fake = backend.get();
    fake->storedMigrationState = IGlobalShortcutBackend::MigrationState::Completed;
    fake->legacyChoice = {legacy};
    fake->setOwners(legacy, {legacyOwner()});

    GlobalShortcutManager manager(&owner, std::move(backend));

    QCOMPARE(fake->ownerLookupCalls, 0);
    QCOMPARE(fake->pendingWriteCalls, 0);
    QCOMPARE(fake->stealCalls, 0);
    QCOMPARE(fake->userOverrideCalls, 0);
    QCOMPARE(fake->legacyChoice, QList<QKeySequence>{legacy});
    QCOMPARE(fake->preserveCalls, 1);
}

QTEST_MAIN(GlobalShortcutTest)
#include "global_shortcut_test.moc"
