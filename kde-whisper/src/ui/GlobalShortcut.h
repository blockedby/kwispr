#pragma once

#include <QKeySequence>
#include <QList>
#include <QString>
#include <memory>

class QAction;
class QObject;

class IGlobalShortcutBackend
{
public:
    enum class Loading {
        PreserveExisting,
        UserOverride,
    };

    virtual ~IGlobalShortcutBackend() = default;

    virtual bool setDefaultShortcut(QAction *action, const QList<QKeySequence> &shortcuts) = 0;
    virtual bool setShortcut(QAction *action,
                             const QList<QKeySequence> &shortcuts,
                             Loading loading) = 0;
    virtual QList<QKeySequence> shortcut(const QAction *action) const = 0;
    virtual bool isShortcutAvailable(const QKeySequence &shortcut,
                                     const QString &componentName) const = 0;
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

private:
    std::unique_ptr<IGlobalShortcutBackend> m_backend;
    QAction *m_action = nullptr;
};
