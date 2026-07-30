cmake_minimum_required(VERSION 3.20)

if(NOT DEFINED SOURCE_DIR OR NOT DEFINED BUILD_DIR)
    message(FATAL_ERROR "SOURCE_DIR and BUILD_DIR are required")
endif()

set(DESKTOP_FILE "${SOURCE_DIR}/org.kwispr.KdeWhisper.desktop")
set(METAINFO_FILE "${SOURCE_DIR}/org.kwispr.KdeWhisper.metainfo.xml")
set(ICON_FILE "${SOURCE_DIR}/icons/org.kwispr.KdeWhisper.svg")

foreach(path IN ITEMS "${DESKTOP_FILE}" "${METAINFO_FILE}" "${ICON_FILE}")
    if(NOT EXISTS "${path}")
        message(FATAL_ERROR "Required metadata file is missing: ${path}")
    endif()
endforeach()

execute_process(
    COMMAND desktop-file-validate "${DESKTOP_FILE}"
    RESULT_VARIABLE desktop_validate_result
    OUTPUT_VARIABLE desktop_validate_out
    ERROR_VARIABLE desktop_validate_err
)
if(NOT desktop_validate_result EQUAL 0)
    message(FATAL_ERROR "desktop-file-validate failed: ${desktop_validate_out}${desktop_validate_err}")
endif()

file(READ "${DESKTOP_FILE}" desktop_text)
if(NOT desktop_text MATCHES "Exec=kde-whisper")
    message(FATAL_ERROR ".desktop must execute kde-whisper")
endif()
if(NOT desktop_text MATCHES "Tray" OR NOT desktop_text MATCHES "Settings")
    message(FATAL_ERROR ".desktop must advertise tray/settings utility")
endif()
if(desktop_text MATCHES "kwispr.sh toggle")
    message(FATAL_ERROR ".desktop must not hijack the existing hotkey/toggle command")
endif()

set(DESTDIR "${BUILD_DIR}/install-metadata-dest")
file(REMOVE_RECURSE "${DESTDIR}")
execute_process(
    COMMAND "${CMAKE_COMMAND}" -E env "DESTDIR=${DESTDIR}" "${CMAKE_COMMAND}" --install "${BUILD_DIR}" --prefix /usr
    RESULT_VARIABLE install_result
    OUTPUT_VARIABLE install_out
    ERROR_VARIABLE install_err
)
if(NOT install_result EQUAL 0)
    message(FATAL_ERROR "cmake --install failed: ${install_out}${install_err}")
endif()

set(expected_files
    "${DESTDIR}/usr/bin/kde-whisper"
    "${DESTDIR}/usr/bin/kwispr"
    "${DESTDIR}/usr/lib/kwispr/kde-whisper"
    "${DESTDIR}/usr/share/kwispr/runtime/kwispr.sh"
    "${DESTDIR}/usr/share/kwispr/runtime/kwispr-models.py"
    "${DESTDIR}/usr/share/kwispr/runtime/models/local-stt-catalog.json"
    "${DESTDIR}/usr/share/kwispr/runtime/sounds/start.wav"
    "${DESTDIR}/usr/share/kwispr/runtime/sounds/stop.wav"
    "${DESTDIR}/usr/share/kwispr/runtime/sounds/ready.wav"
    "${DESTDIR}/usr/share/applications/org.kwispr.KdeWhisper.desktop"
    "${DESTDIR}/usr/share/metainfo/org.kwispr.KdeWhisper.metainfo.xml"
    "${DESTDIR}/usr/share/icons/hicolor/scalable/apps/org.kwispr.KdeWhisper.svg"
)
foreach(path IN LISTS expected_files)
    if(NOT EXISTS "${path}")
        message(FATAL_ERROR "Installed file missing: ${path}")
    endif()
endforeach()

execute_process(
    COMMAND "${CMAKE_COMMAND}" -E env
        "XDG_DATA_HOME=${BUILD_DIR}/install-metadata-data"
        "${DESTDIR}/usr/bin/kwispr" models list
    RESULT_VARIABLE installed_cli_result
    OUTPUT_VARIABLE installed_cli_out
    ERROR_VARIABLE installed_cli_err
)
if(NOT installed_cli_result EQUAL 0 OR installed_cli_out STREQUAL "")
    message(FATAL_ERROR "Installed relocatable CLI failed: ${installed_cli_out}${installed_cli_err}")
endif()

# A nested bindir and non-default lib/data directories must still resolve the
# prefix from the launcher's installed location.
set(NESTED_BUILD "${BUILD_DIR}/nested-layout-build")
set(NESTED_DEST "${BUILD_DIR}/nested-layout-dest")
file(REMOVE_RECURSE "${NESTED_BUILD}" "${NESTED_DEST}")
execute_process(
    COMMAND "${CMAKE_COMMAND}"
        -S "${SOURCE_DIR}"
        -B "${NESTED_BUILD}"
        -DBUILD_TESTING=OFF
        -DCMAKE_INSTALL_BINDIR=libexec/kwispr-bin
        -DCMAKE_INSTALL_LIBDIR=lib64
        -DCMAKE_INSTALL_DATADIR=share-data
    RESULT_VARIABLE nested_configure_result
    OUTPUT_VARIABLE nested_configure_out
    ERROR_VARIABLE nested_configure_err
)
if(NOT nested_configure_result EQUAL 0)
    message(FATAL_ERROR "Nested-layout configure failed: ${nested_configure_out}${nested_configure_err}")
endif()
file(COPY "${BUILD_DIR}/kde-whisper" DESTINATION "${NESTED_BUILD}")
execute_process(
    COMMAND "${CMAKE_COMMAND}" -E env "DESTDIR=${NESTED_DEST}"
        "${CMAKE_COMMAND}" --install "${NESTED_BUILD}" --prefix /opt/kwispr
    RESULT_VARIABLE nested_install_result
    OUTPUT_VARIABLE nested_install_out
    ERROR_VARIABLE nested_install_err
)
if(NOT nested_install_result EQUAL 0)
    message(FATAL_ERROR "Nested-layout install failed: ${nested_install_out}${nested_install_err}")
endif()
execute_process(
    COMMAND "${CMAKE_COMMAND}" -E env
        "XDG_DATA_HOME=${BUILD_DIR}/nested-layout-data"
        "${NESTED_DEST}/opt/kwispr/libexec/kwispr-bin/kwispr" models list
    RESULT_VARIABLE nested_cli_result
    OUTPUT_VARIABLE nested_cli_out
    ERROR_VARIABLE nested_cli_err
)
if(NOT nested_cli_result EQUAL 0 OR nested_cli_out STREQUAL "")
    message(FATAL_ERROR "Nested-layout launcher failed: ${nested_cli_out}${nested_cli_err}")
endif()
