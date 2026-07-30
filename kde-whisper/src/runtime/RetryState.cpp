#include "runtime/RetryState.h"

QString retryWavPathFromState(QString state)
{
    state = state.trimmed();
    const QString retryMarker = QStringLiteral(" retry ");
    const qsizetype markerIndex = state.indexOf(retryMarker);
    if (markerIndex < 0) {
        return state;
    }

    state = state.mid(markerIndex + retryMarker.size()).trimmed();
    if (state.size() >= 2
        && ((state.startsWith(QLatin1Char('"')) && state.endsWith(QLatin1Char('"')))
            || (state.startsWith(QLatin1Char('\'')) && state.endsWith(QLatin1Char('\''))))) {
        state = state.mid(1, state.size() - 2);
    }
    return state;
}
