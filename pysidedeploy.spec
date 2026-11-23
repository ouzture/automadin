[app]

# title of your application
title = Automadin

# project root directory. default = The parent directory of input_file
project_dir = __AUTOMADIN_PROJECT_DIR__

# source file entry point path. default = main.py
input_file = main.py

# directory where the executable output is generated
exec_directory = dist

# path to the project file relative to project_dir
project_file =

# application icon
icon = automadin/resources/icon.ico

[python]

# python path
python_path = __AUTOMADIN_PYTHON__

# python packages to install
packages = Nuitka==2.7.11

# buildozer = for deploying Android application
android_packages = buildozer==1.5.0,cython==0.29.33

[qt]

# paths to required qml files. comma separated
# normally all the qml files required by the project are added automatically
# design studio projects include the qml files using qt resources
qml_files =

# excluded qml plugin binaries
excluded_qml_plugins =

# qt modules used. comma separated
modules = Core,Gui,Widgets,Network

# qt plugins used by the application. only relevant for desktop deployment
# for qt plugins used in android application see [android][plugins]
plugins = accessiblebridge,egldeviceintegrations,generic,iconengines,imageformats,platforminputcontexts,platforms,platforms/darwin,platformthemes,styles,xcbglintegrations

[android]

# path to pyside wheel
wheel_pyside =

# path to shiboken wheel
wheel_shiboken =

# plugins to be copied to libs folder of the packaged application. comma separated
plugins =

[nuitka]

# usage description for permissions requested by the app as found in the info.plist file
# of the app bundle. comma separated
# eg = extra_args = --show-modules --follow-stdlib
macos.permissions =

# mode of using nuitka. accepts standalone or onefile. default = onefile
mode = standalone

# specify any extra nuitka arguments
extra_args = --quiet --assume-yes-for-downloads --noinclude-qt-translations --nofollow-import-to=cv2.gapi --windows-console-mode=disable --company-name=Automadin --product-name=Automadin --file-description=Automadin --file-version=__AUTOMADIN_VERSION__ --product-version=__AUTOMADIN_VERSION__ --include-package-data=automadin --include-package-data=playwright --include-distribution-metadata=playwright --include-distribution-metadata=PySide6 --include-distribution-metadata=windows-cap --include-package=windows_cap --include-module=automadin.browser_health_worker --include-module=automadin.release_self_test --include-module=automadin.diagnostics_self_test --include-module=automadin.pushover_self_test --include-module=automadin.support_report_worker --include-module=automadin.services.support_report_process --include-module=win32api --include-module=win32event --include-module=win32job --include-module=win32con --include-module=win32cred --include-module=win32timezone --include-module=win32process --include-module=win32gui --include-module=win32ui --include-module=pywintypes

[buildozer]

# build mode
# possible values = ["aarch64", "armv7a", "i686", "x86_64"]
# release creates a .aab, while debug creates a .apk
mode = debug

# path to pyside6 and shiboken6 recipe dir
recipe_dir =

# path to extra qt android .jar files to be loaded by the application
jars_dir =

# if empty, uses default ndk path downloaded by buildozer
ndk_path =

# if empty, uses default sdk path downloaded by buildozer
sdk_path =

# other libraries to be loaded at app startup. comma separated.
local_libs =

# architecture of deployed platform
arch =
