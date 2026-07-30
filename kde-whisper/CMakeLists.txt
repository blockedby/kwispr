cmake_minimum_required(VERSION 3.20)
project(KdeWhisper VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)

include(CTest)
include(GNUInstallDirs)

foreach(install_dir IN ITEMS CMAKE_INSTALL_BINDIR CMAKE_INSTALL_LIBDIR CMAKE_INSTALL_DATADIR)
    if(IS_ABSOLUTE "${${install_dir}}")
        message(FATAL_ERROR "An absolute ${install_dir} is not supported by the relocatable launchers")
    endif()
endforeach()
file(RELATIVE_PATH KWISPR_BINDIR_TO_PREFIX "/${CMAKE_INSTALL_BINDIR}" "/")
configure_file(scripts/kwispr.in "${CMAKE_CURRENT_BINARY_DIR}/kwispr" @ONLY)
configure_file(scripts/kde-whisper.in "${CMAKE_CURRENT_BINARY_DIR}/kde-whisper-launcher" @ONLY)

find_package(ECM 6.0 REQUIRED NO_MODULE)
set(CMAKE_MODULE_PATH ${ECM_MODULE_PATH})

find_package(Qt6 REQUIRED COMPONENTS Core Widgets Test Network)
find_package(KF6 REQUIRED COMPONENTS CoreAddons StatusNotifierItem)

add_library(kde_whisper_core STATIC
    src/AppMetadata.cpp
    src/config/EnvFile.cpp
    src/config/KwisprSettings.cpp
    src/runtime/KwisprController.cpp
    src/runtime/LocalSttClient.cpp
    src/runtime/ProcessRunner.cpp
    src/runtime/RetryState.cpp
    src/models/ModelCatalog.cpp
    src/models/ModelManager.cpp
    src/ui/SettingsDialog.cpp
    src/ui/TrayApp.cpp
    src/ui/TrayController.cpp
)
target_include_directories(kde_whisper_core PUBLIC src)
target_link_libraries(kde_whisper_core PUBLIC Qt6::Core Qt6::Network Qt6::Widgets KF6::StatusNotifierItem)

add_executable(kde-whisper
    src/main.cpp
)
target_link_libraries(kde-whisper PRIVATE kde_whisper_core Qt6::Widgets KF6::CoreAddons)

install(TARGETS kde-whisper RUNTIME DESTINATION "${CMAKE_INSTALL_LIBDIR}/kwispr")
install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/kwispr"
    DESTINATION ${CMAKE_INSTALL_BINDIR})
install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/kde-whisper-launcher"
    DESTINATION ${CMAKE_INSTALL_BINDIR}
    RENAME kde-whisper)
install(PROGRAMS
    "${CMAKE_CURRENT_SOURCE_DIR}/../kwispr.sh"
    "${CMAKE_CURRENT_SOURCE_DIR}/../kwispr-models.py"
    DESTINATION "${CMAKE_INSTALL_DATADIR}/kwispr/runtime")
install(FILES
    "${CMAKE_CURRENT_SOURCE_DIR}/../models/local-stt-catalog.json"
    DESTINATION "${CMAKE_INSTALL_DATADIR}/kwispr/runtime/models")
install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../sounds/"
    DESTINATION "${CMAKE_INSTALL_DATADIR}/kwispr/runtime/sounds")
install(FILES org.kwispr.KdeWhisper.desktop DESTINATION ${CMAKE_INSTALL_DATADIR}/applications)
install(FILES org.kwispr.KdeWhisper.metainfo.xml DESTINATION ${CMAKE_INSTALL_DATADIR}/metainfo)
install(FILES icons/org.kwispr.KdeWhisper.svg DESTINATION ${CMAKE_INSTALL_DATADIR}/icons/hicolor/scalable/apps)

if(BUILD_TESTING)

    add_test(NAME DocsCheck
        COMMAND ${CMAKE_COMMAND}
            -DSOURCE_DIR=${CMAKE_SOURCE_DIR}
            -P ${CMAKE_SOURCE_DIR}/tests/docs_check.cmake)

    add_test(NAME InstallMetadataTest
        COMMAND ${CMAKE_COMMAND}
            -DSOURCE_DIR=${CMAKE_SOURCE_DIR}
            -DBUILD_DIR=${CMAKE_BINARY_DIR}
            -P ${CMAKE_SOURCE_DIR}/tests/install_metadata_test.cmake)
    add_executable(TrayControllerTest tests/tray_controller_test.cpp)
    target_link_libraries(TrayControllerTest PRIVATE kde_whisper_core Qt6::Test Qt6::Widgets)
    add_test(NAME TrayControllerTest COMMAND TrayControllerTest)
    set_tests_properties(TrayControllerTest PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen")

    add_executable(TrayAppTest tests/tray_app_test.cpp)
    target_link_libraries(TrayAppTest PRIVATE kde_whisper_core Qt6::Test Qt6::Widgets)
    add_test(NAME TrayAppTest COMMAND TrayAppTest)
    set_tests_properties(TrayAppTest PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen")

    add_executable(SettingsDialogTest tests/settings_dialog_test.cpp)
    target_link_libraries(SettingsDialogTest PRIVATE kde_whisper_core Qt6::Test Qt6::Widgets)
    add_test(NAME SettingsDialogTest COMMAND SettingsDialogTest)
    set_tests_properties(SettingsDialogTest PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen")

    add_executable(LocalSttClientTest tests/local_stt_client_test.cpp)
    target_link_libraries(LocalSttClientTest PRIVATE kde_whisper_core Qt6::Test Qt6::Network)
    add_test(NAME LocalSttClientTest COMMAND LocalSttClientTest)

    add_executable(ModelManagerTest tests/model_manager_test.cpp)
    target_link_libraries(ModelManagerTest PRIVATE kde_whisper_core Qt6::Test)
    add_test(NAME ModelManagerTest COMMAND ModelManagerTest)
    set_tests_properties(ModelManagerTest PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}/..)

    add_executable(ProcessRunnerTest tests/process_runner_test.cpp)
    target_link_libraries(ProcessRunnerTest PRIVATE kde_whisper_core Qt6::Test)
    add_test(NAME ProcessRunnerTest COMMAND ProcessRunnerTest)

    add_executable(SettingsModelTest tests/settings_model_test.cpp)
    target_link_libraries(SettingsModelTest PRIVATE kde_whisper_core Qt6::Test)
    add_test(NAME SettingsModelTest COMMAND SettingsModelTest)

    add_executable(EnvFileTest tests/env_file_test.cpp)
    target_link_libraries(EnvFileTest PRIVATE kde_whisper_core Qt6::Test)
    add_test(NAME EnvFileTest COMMAND EnvFileTest)

    add_executable(ScaffoldTest tests/scaffold_test.cpp)
    target_link_libraries(ScaffoldTest PRIVATE kde_whisper_core Qt6::Test)
    add_test(NAME ScaffoldTest COMMAND ScaffoldTest)
endif()
