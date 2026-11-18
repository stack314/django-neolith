"""The settings module Django is pointed at while the command line reads the
deployer's module and the apps' declarations, before settings exist
(ADR 0022 Decision 1). Importing it raises, so a read of a setting then is
refused by name, instead of with Django's advice to set
``DJANGO_SETTINGS_MODULE``, which the command line does not read.
"""

from neolith._cli import _SettingRead

raise _SettingRead
