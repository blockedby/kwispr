#include "fake_global_shortcut.h"
#include "ui/GlobalShortcut.h"

#include <QAction>
#include <QObject>
#include <QtTest/QtTest>
#include <memory>

class GlobalShortcutTest : public QObject
{
    Q_OBJECT
private slots:
    void freshRegistrationUsesDefaultOnlyWhenAvailable();
    void savedCustomAndClearedChoicesArePreserved();
    void customAndClearedChoicesPersistImmediately();
    void conflictFailsWithoutMutation();
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
    const QKeySequence custom(QStringLiteral("Meta+Alt+D"));
    QObject customOwner;
    auto customBackend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *customFake = customBackend.get();
    customFake->savedChoiceExists = true;
    customFake->savedChoice = {custom};
    GlobalShortcutManager customManager(&customOwner, std::move(customBackend));
    QCOMPARE(customManager.currentShortcut(), custom);
    QCOMPARE(customFake->userOverrideCalls, 0);

    QObject clearedOwner;
    auto clearedBackend = std::make_unique<FakeGlobalShortcutBackend>();
    auto *clearedFake = clearedBackend.get();
    clearedFake->savedChoiceExists = true;
    GlobalShortcutManager clearedManager(&clearedOwner, std::move(clearedBackend));
    QVERIFY(clearedManager.currentShortcut().isEmpty());
    QCOMPARE(clearedFake->userOverrideCalls, 0);
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

QTEST_MAIN(GlobalShortcutTest)
#include "global_shortcut_test.moc"
