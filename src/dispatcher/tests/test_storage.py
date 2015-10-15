#+
# Copyright 2014 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
######################################################################

import unittest
from dispatcher.rpc import RpcException
from shared import BaseTestCase
import os


class VolumeTest(BaseTestCase):
    def setUp(self):
        super(VolumeTest, self).setUp()
        
        self.task_timeout = 100

    def tearDown(self):
        # try to delete all volumes created with test
        for u in self.conn.call_sync('volumes.query', [('name', '~', 'Test*')]):
            self.assertTaskCompletion(self.submitTask('volume.destroy', u['name']))
        super(VolumeTest, self).tearDown()

    def test_query_volumes(self):
        volumes = self.conn.call_sync('volumes.query', [])
        self.assertIsInstance(volumes, list)
        for v in volumes:
            print v['name']

    def test_create_volume_auto_stripe(self):
        '''
        Create, test, destroy
        '''
        volname = 'testVolumeAuto'
        v =  self.conn.call_sync('volumes.query', [('name', '=', volname)])

        if len(v):
            tid = self.submitTask('volume.destroy', volname)
            self.assertTaskCompletion(tid)

        exported =  self.conn.call_sync('volumes.find', [('name', '=', volname)])    
        print exported

        available = self.conn.call_sync('volumes.get_available_disks')    
        if available:
            tid = self.submitTask('volume.create_auto', volname, 'zfs', available[:1])
            
            self.assertTaskCompletion(tid)
        else:
            print "No disks are available for creating volume, test will not run"  

        
    def test_create_volume_auto_available_disks(self):
        volname = 'TestVolumeAuto'
        v =  self.conn.call_sync('volumes.query', [('name', '=', volname)])
        if len(v):
            tid = self.submitTask('volume.destroy', volname)
            self.assertTaskCompletion(tid)
        
        available = self.conn.call_sync('volumes.get_available_disks')
        if not available:
            print "No disks are available for creating volume, test will not run"
        else:
            tid = self.submitTask('volume.create_auto', volname, 'zfs', available)
            self.assertTaskCompletion(tid)

    def test_create_stripe(self):
        volname = "TestVolume"
        v =  self.conn.call_sync('volumes.query', [('name', '=', volname)])
        if len(v):
            tid = self.submitTask('volume.destroy', volname)
            self.assertTaskCompletion(tid)
        
        available = self.conn.call_sync('volumes.get_available_disks')
        if available:
            vdevs =  [{'type': 'disk', 'path': str(available[0])}]
            payload = {
                "name": volname,
                "type": 'zfs',
                "topology": {'data': vdevs},                    
            }
            tid = self.submitTask('volume.create', payload)
            self.assertTaskCompletion(tid)
        else:
            print "No disks are available for creating volume, test will not run"     
                

    def test_create_mirror(self):
        volname = "TestVolumeMirror"
        v =  self.conn.call_sync('volumes.query', [('name', '=', volname)])
        if len(v):
            tid = self.submitTask('volume.destroy', volname)
            self.assertTaskCompletion(tid)
        
        available = self.conn.call_sync('volumes.get_available_disks')
        print available
        if len(available) >= 2:   
            vdevs =  [
            {'type': 'disk', 'path': str(available[0])}, 
            {'type': 'disk', 'path': str(available[1])} ]
            payload = {
                "name": volname,
                "type": 'zfs',
                "topology": {'data': vdevs},                    
            }
            tid = self.submitTask('volume.create', payload)
            self.assertTaskCompletion(tid)
        else:
            print "No disks are available for creating volume, test will not run"     


    def test_create_RAIDZ(self):
        volname = "TestVolume"
        v =  self.conn.call_sync('volumes.query', [('name', '=', volname)])
        if len(v):
            tid = self.submitTask('volume.destroy', volname)
            self.assertTaskCompletion(tid)
        available = self.conn.call_sync('volumes.get_available_disks')

        if len(available) < 3:
            print "No disks are available for creating volume, test will not run"     
            
        else:    
            vdevs =  [{'type': 'disk', 'path': str(available[0])}, 
            {'type': 'disk', 'path': (available[1])},
            {'type': 'disk', 'path': (available[2])}]
            payload = {
                "name": volname,
                "type": 'zfs',
                "topology": {'data': vdevs},                    
            }
            tid = self.submitTask('volume.create', payload)
            self.assertTaskCompletion(tid)        
        

    def get_all_disks(self):
        disks = self.conn.call_sync('disks.query')
        for disk in disks:
            for d in disk.keys():
                print str(d) + ':  ' + str(disk[d])

    def get_available_disks(self):
        disks = self.conn.call_sync('volumes.get_available_disks')
        return disks   

    def find_volume(self):
        available = self.conn.call_sync('volumes.find')
        return available             
        
    def atest_get_disk_path(self, disk):
        disks = self.conn.call_sync('volumes.get_disks_allocation')
        print disks  
    

    def atest_detach_reimport_all_volumes(self):
        # detach all volumes created with test
        vols = self.conn.call_sync('volumes.query')
        for v in vols:
            print 'Detaching ' + str(v['name'])
            tid = self.submitTask('volume.detach', v['name'])
        detached = self.conn.call_sync('volumes.find')
        for v in detached:
            if not v['status'] == 'DEGRADED':
                payload = [{'id': str(v['id']), 'new_name': 'new_' + str(v['name']), 'params': {} }]
                tid = self.submitTask('volume.import', payload)
            imported =  self.conn.call_sync('volumes.query', [('name', '=', v['name'])])    
                    

    def atest_create_manual_snapshot(self):
        pass    

    def atest_import_disk(self):
        pass
 

class DatasetTest(BaseTestCase):
    def setUp(self):
        super(DatasetTest, self).setUp()
        self.task_timeout = 200

    def tearDown(self):
        # try to delete all volumes created with test
        #for u in self.conn.call_sync('volumes.query', [('volume', '~', 'testVolume.*')]):
        #    self.assertTaskCompletion(self.submitTask('volume.detach', u['name']))
        super(DatasetTest, self).tearDown()

    def test_query_datasets(self):
        pass

    def test_create_dataset(self):
        pass



if __name__ == '__main__':
    unittest.main()
