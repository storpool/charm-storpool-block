#!/usr/bin/python

from __future__ import print_function

import os
import re
import requests
import subprocess
import yaml
import sys

base_url='https://github.com/storpool'
subdir='storpool-charms'
re_elem = re.compile('(?P<type> (?: layer | interface ) ) : (?P<name> [a-z][a-z-]* ) $', re.X)

def checkout_repository(name):
	url = '{base}/{name}.git'.format(base=base_url, name=name)
	try:
		if not requests.request('GET', url).ok:
			exit('The {name} StorPool repository does not seem to exist on GitHub!'.format(name=name))
	except Exception as e:
		exit('Could not check for the existence of the {name} StorPool repository on GitHub: {e}'.format(name=name, e=e))
	print('Checking out {url}'.format(url=url))
	try:
		subprocess.check_call(['git', 'clone', '--', url]);
	except Exception:
		exit('Could not check out the {name} module'.format(name=name))

def is_file_not_found(e):
	try:
		return isinstance(e, FileNotFoundError)
	except NameError:
		return (isinstance(e, IOError) or isinstance(e, OSError)) and e.errno == os.errno.ENOENT

def checkout_recursive(name, layers_required=False):
	checkout_repository(name)
	# This is done mainly for the "../" in the loop below, but oh well.
	os.chdir(name)

	try:
		with open('layer.yaml', mode='r') as f:
			contents = yaml.load(f)
	except Exception as e:
		if is_file_not_found(e) and not layers_required:
			return
		exit('Could not load the layer.yaml file from {name}: {e}'.format(name=name, e=e))

	for elem in filter(lambda e: e.find('storpool') != -1, contents['includes']):
		m = re_elem.match(elem)
		if m is None:
			exit('Invalid value "{elem}" in the {name} "includes" directive!'.format(name=name, elem=elem))
		(e_type, e_name) = (m.groupdict()['type'], m.groupdict()['name'])

		print('Processing {t} "{n}"'.format(t=e_type, n=e_name))
		os.chdir('../../{t}s'.format(t=e_type))
		dname = '{t}-{n}'.format(t=e_type, n=e_name)

		if os.path.exists(dname):
			if not os.path.isdir(dname):
				exit('Something named {t}/{n} exists and it is not a directory!'.format(t=e_type, n=e_name))
			print('The {n} {t} has already been checked out.'.format(n=e_name, t=e_type))
			os.chdir(dname)
		else:
			checkout_recursive('{t}-{n}'.format(t=e_type, n=e_name))

def checkout_charm_recursive(name):
	print('Checking out the {name} charm and its dependencies'.format(name=name))
	checkout_recursive(name, layers_required=True)
	os.chdir('../../charms')
	print('Done with the {name} charm!'.format(name=name))
	print('')

if len(sys.argv) != 2:
	exit('Usage: storpool-charms path/to/parent/dir\nExample: storpool-charms .\nA {subdir} subdirectory will be created in the specified directory.'.format(subdir=subdir))
d = sys.argv[1]
try:
	os.chdir(d)
except Exception as e:
	if is_file_not_found(e):
		exit('The {d} directory does not seem to exist!'.format(d=d))
	raise

print('Recreating the {subdir}/ tree'.format(subdir=subdir))
subprocess.check_call(['rm', '-rf', '--', subdir])
os.mkdir(subdir)
os.chdir(subdir)
for comp in ('layers', 'interfaces', 'charms'):
	os.mkdir(comp)
os.chdir('charms')

checkout_charm_recursive('charm-storpool-block')
checkout_charm_recursive('charm-cinder-storpool')
checkout_charm_recursive('charm-storpool-inventory')

print('All done!')
print('')

print('###################################################')
print('')
print('#!/bin/sh')
print('')
print('set -e')
print('')

os.chdir('../layers')
print("export LAYER_PATH='{path}'".format(path=os.getcwd()))
os.chdir('../interfaces')
print("export INTERFACE_PATH='{path}'".format(path=os.getcwd()))
print('')

os.chdir('../charms/charm-storpool-block')
print("cd -- '{path}'".format(path=os.getcwd()))
print('make && make deploy')
print('')

os.chdir('../charm-cinder-storpool')
print("cd -- '{path}'".format(path=os.getcwd()))
print('make && make deploy')
print('')

os.chdir('../charm-storpool-inventory')
print("cd -- '{path}'".format(path=os.getcwd()))
print('make && make deploy')
print('')
