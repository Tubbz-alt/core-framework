#
# This file is protected by Copyright. Please refer to the COPYRIGHT file 
# distributed with this source distribution.
# 
# This file is part of REDHAWK core.
# 
# REDHAWK core is free software: you can redistribute it and/or modify it under 
# the terms of the GNU Lesser General Public License as published by the Free 
# Software Foundation, either version 3 of the License, or (at your option) any 
# later version.
# 
# REDHAWK core is distributed in the hope that it will be useful, but WITHOUT 
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS 
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
# 
# You should have received a copy of the GNU Lesser General Public License 
# along with this program.  If not, see http://www.gnu.org/licenses/.
#

BasicTestDevice_java.jar$(EXEEXT): $(BasicTestDevice_java_jar_SOURCES)
	mkdir -p bin
	$(JAVAC) -cp $(OSSIE_CLASSPATH) -d bin $(BasicTestDevice_java_jar_SOURCES)
	$(JAR) cf ./BasicTestDevice_java.jar -C bin .

clean-local:
	rm -rf bin

ossieName = BasicTestDevice_java
bindir = $(prefix)/dev/devices/BasicTestDevice_java/java/
noinst_PROGRAMS = BasicTestDevice_java.jar
BasicTestDevice_java_jar_SOURCES := $(shell find ./src -name "*.java")

xmldir = $(prefix)/dev/devices/BasicTestDevice_java/

devdir = $(prefix)/dev/devices/BasicTestDevice_java/java/
