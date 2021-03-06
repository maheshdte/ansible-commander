#!/usr/bin/env python

# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import sqlite3
import ConfigParser
import json
import time

# TODO: make a seperate config class
parser  = ConfigParser.ConfigParser()
parser.read("/etc/ansible/commander.cfg")

dbfile  = parser.get('database', 'file')

# this code is used such that only certain hosts will be able to view
# inventory variables (since inventory scripts don't have to log in,
# we do require that they know this code)

TESTMODE=False

def connect(testmode=False):
   global dbfile
   if TESTMODE:
      dbfile = "%s_test" % dbfile
   return sqlite3.connect(dbfile + '.db', check_same_thread = False)

conn = connect()

def test_mode():
   global conn
   global TESTMODE
   TESTMODE=True
   conn.close()
   conn = connect(testmode=True)

class DataException(Exception):
   pass

class AlreadyExists(DataException):
   pass

class InvalidInput(DataException):
   pass

class DoesNotExist(DataException):
   pass

class Ambigious(DataException):
   pass

class Base(object):

    def __init__(self):
        pass

    def compute_derived_fields_on_edit(self, name, results):
        pass

    def compute_derived_fields_on_add(self, name, results):
        pass

    def cursor(self):
        return conn.cursor()

    def check_required_fields(self, fields, edit=False, internal=False):

        if internal and TESTMODE and not 'TESTMODE' in self.FIELDS['protected']:
            self.FIELDS['protected'].append('TESTMODE')
            fields['TESTMODE'] = 1

        if 'id' in fields:
            fields.pop('id')

        # do not allow users to edit protected fields
        if not internal: 
            for p in self.FIELDS['protected']:
                if p in fields:
                    if p == '_salt':
                        raise Exception("popping: %s" % p)
                    fields.pop(p) 

        if not edit:
            if fields.get(self.FIELDS['primary'], None) is None:
                raise InvalidInput("missing primary field: %s" % self.FIELDS['primary'])
     
            # all required fields are set
            for f in self.FIELDS['required']:
                if f not in fields:
                    raise InvalidInput("field %s is required" % f)

            # any optional fields get set to defaults if missing
            for f in self.FIELDS['optional']:
                if f not in fields:
                    fields[f] = self.FIELDS['optional'][f] 

        # no unexpected fields
        for f in fields:
            if internal and f in self.FIELDS['protected']:
                continue
            if f not in self.FIELDS['required'] and f not in self.FIELDS['optional'] and f != self.FIELDS['primary']:
                raise InvalidInput("invalid field %s" % f)

    def add(self, properties, hook=False):

        if 'href' in properties:
            properties.pop('href')

        primary = self.FIELDS['primary']
        if not primary in properties:
            raise InvalidInput("missing value for name field: %s" % primary)
        name = properties[primary]
        self.check_required_fields(properties)
        try:
            match = self.lookup(properties[primary])
            raise AlreadyExists()
        except DoesNotExist:
            pass
        cur = self.cursor()
        sth = """
            INSERT INTO thing (type) VALUES (?)
        """
        cur.execute(sth, [self.TYPE])
        tid = cur.lastrowid

        inserts = []
        for (k,v) in properties.iteritems():
            inserts.append((tid, k, json.dumps(v)))

        sth = """
            INSERT INTO properties (thing_id,key,value) VALUES(?, ?, ?)
        """
        cur.executemany(sth, inserts)
        conn.commit()
        match = self.lookup(properties[primary])
        if not hook:
            self.compute_derived_fields_on_add(name, match)
        return match

    def edit(self, name, properties, internal=False, hook=False):

        if 'href' in properties:
            properties.pop('href')

        primary = self.FIELDS['primary']
        self.check_required_fields(properties, edit=True, internal=internal)
 
        if primary in properties and properties[primary] != name:
            raise Exception("renames are not supported, delete and re-add")

        match = self.lookup(name, internal=True)
        id = match['id']

        for (k,v) in properties.iteritems():
            if k not in match:
                # TODO: would be nice to execute many here
                self._insert_kv(id,k,v)
            elif match[k] != properties[k]:
                self._update_kv(id,k,v)
        
        match = self.lookup(name, internal=True)
        if not hook:
            self.compute_derived_fields_on_edit(name, match)
        match = self.lookup(name, internal=internal)
        return match

    def _insert_kv(self, id, k, v):
        cur = self.cursor()
        v=json.dumps(v)
        sth = """
           INSERT INTO properties (thing_id, key, value)
           VALUES (%s,%s,%s)
        """
        cur.execute(sth, [id,k,v])
        conn.commit()

    def _update_kv(self, id, k, v):
        cur = self.cursor()
        v=json.dumps(v)
        sth = """
            UPDATE properties 
            SET 
               value=%s
            WHERE
               id IN (
                    SELECT id FROM properties
                    WHERE thing_id=%s
                    AND key=%s
               )
        """
        cur.execute(sth,[v,id,k])
        conn.commit()
 
    def list(self, internal=False):
        cur = self.cursor()
        sth = """
             SELECT t.id, p.id, p.key, p.value 
             FROM thing t
             LEFT JOIN properties p
             ON p.thing_id = t.id
             WHERE t.type = ?
        """

        cur.execute(sth, [self.TYPE])
        db_results = cur.fetchall()
        return self._reformat(db_results, internal=internal)

    def _reformat(self, db_results, internal=False):

        results = {}
        for (tid, pid, key, value) in db_results:
            if not tid in results:
                results[tid] = {}
            results[tid]['id'] = tid
            if internal or (key not in self.FIELDS['private'] and key not in self.FIELDS['hidden']):
                if value is not None:
                    results[tid][key] = json.loads(value) 
            if self.REST is not None and key == self.FIELDS['primary']:
                results[tid]['href'] = self.REST % json.loads(value)
        return results.values()
    
    def get_by_id(self, id, internal=False, allow_missing=False):
        cur = self.cursor()
        sth = """
             SELECT t.id, p.id, p.key, p.value
             FROM thing t 
             LEFT JOIN properties p
             ON t.id = p.thing_id
             WHERE t.id = %s
             AND t.type= %s
        """
        cur.execute(sth, [id,self.TYPE])
        db_results = cur.fetchall()
        results = self._reformat(db_results, internal=internal)
        if len(results) == 0:
            if allow_missing:
                return None
            else:
                raise DoesNotExist()
        if len(results) > 1:
            raise Ambigious()
        return results[0]

    
    def find(self, key, value, internal=False, expect_one=False):
        cur = self.cursor()
        # all values are stored in JSON in the DB, so ookups must also jsonify first
        value = json.dumps(value)
        sth = """
             SELECT t.id, p.id, p.key, p.value 
             FROM thing t
             LEFT JOIN properties p
             ON p.thing_id = t.id 
             WHERE t.type = %s
             AND t.id IN (
                 SELECT tt.id
                    FROM thing tt, properties pp
                    WHERE pp.thing_id = tt.id
                    AND tt.type  = %s
                    AND pp.key   = %s
                    AND pp.value = %s
             )
        """

        cur.execute(sth, [self.TYPE,self.TYPE,key,value])
        db_results = cur.fetchall()
 
        results = self._reformat(db_results, internal=internal)
        if not expect_one:
            return results
        else:
            if len(results) == 0:
                raise DoesNotExist("%s/%s=%s" % (self.TYPE,key,value))
            if len(results) > 1:
                raise Ambigious()
            return results[0]

    def lookup(self, value, internal=False):
        return self.find(self.FIELDS['primary'], value, internal=internal, expect_one=True)    
 
    def delete(self, value):
        cur = self.cursor()
        obj = self.find(self.FIELDS['primary'], value)    
        if len(obj) == 0:
            # delete on something that doesn't exist is fine
            return
        elif len(obj) != 1:
            raise Ambigious()
        id = obj[0]['id']
        sth = """
           DELETE FROM thing where id=%s
        """
        cur.execute(sth, [id])
        conn.commit()
        return dict()

    def clear_test_data(self):
        if not TESTMODE:
            raise Exception("only supported in TESTMODE")
        cur = self.cursor()
        sth = """
            DELETE FROM thing WHERE type = ?
        """
        cur.execute(sth, [self.TYPE])
        conn.commit()
            
