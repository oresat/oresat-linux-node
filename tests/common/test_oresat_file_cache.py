#uncomment next two lines to run test within the same directory
#import sys
#sys.path.append('../../')

import unittest

import threading

from time import sleep

from shutil import rmtree

from os import rmdir, remove, listdir

from os.path import basename, abspath, exists

from olaf.common.oresat_file_cache import OreSatFileCache

from olaf.common.oresat_file import new_oresat_file

class TestOreSatFileCache(unittest.TestCase):

    def test_oresat_file_cache__init__(self):
        #TEST: non-existing directory name
        dir_name = 'delete_me'
        result = OreSatFileCache(dir_name)
        self.assertEqual(result._dir, abspath(dir_name) + '/') 
        self.assertEqual(result.dir, abspath(dir_name)) #uses property 'def dir(self) -> str:'
        self.assertFalse(result._data)  #empty list
        self.assertTrue(result._lock)   #checks if Lock() object instance exists
        self.assertEqual(OreSatFileCache.__len__(result), 0)     #tests __len__ function
        rmdir(abspath(dir_name))
        
        #TEST: non-existing file name used as dir name
        dir_name = 'delete_me.txt'
        result = OreSatFileCache(dir_name)
        self.assertEqual(result._dir, abspath(dir_name) + '/') 
        self.assertEqual(result.dir, abspath(dir_name))
        self.assertFalse(result._data)
        self.assertTrue(result._lock)
        self.assertEqual(OreSatFileCache.__len__(result), 0)
        rmdir(abspath(dir_name))

        #TEST: existing directory name
        dir_name = 'existing_dir'
        OreSatFileCache(dir_name)               #create directory 'existing_dir'
        result = OreSatFileCache(dir_name)      #create directory with same name
        self.assertEqual(result._dir, abspath(dir_name) + '/') 
        self.assertEqual(result.dir, abspath(dir_name))
        self.assertFalse(result._data)
        self.assertTrue(result._lock)
        self.assertEqual(OreSatFileCache.__len__(result), 0)
        rmdir(abspath(dir_name))

        #TEST: existing file name used as dir name
        dir_name = 'existing_file.xyz'
        with open(dir_name, 'w') as fp:
            pass
        OreSatFileCache(dir_name) #catches error creating directory with file name
        remove(abspath(dir_name))

        
    def test_oresat_file_cache_add(self):

        #TEST: populate directory with GOOD file name conventions
        dir_name = 'good_cache'
        result = OreSatFileCache(dir_name)

        file_names = ['good_file_12345.txt','good_file_12345.tar.xz','good_file_123']
        for name in file_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, True) #True == consume the file
            self.assertFalse(exists(abspath(name))) #exists == False, file has been consumed

        file_names = ['good_file_67890.txt','good_file_67890.tar.xz','good_file_678']
        for name in file_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, False) #False == does not consume the file
            self.assertTrue(exists(abspath(name))) #exists == True, original copy of file exists
            remove(abspath(name))

        #adding file via path
        with open('good_file_03152022', 'w') as fp:
            pass
        file_path = abspath('good_file_03152022')
        OreSatFileCache.add(result, file_path, True)

        #Raises: FileNotFoundError
        file_path = abspath('bad_path_123.txt')
        with self.assertRaises(FileNotFoundError):
            OreSatFileCache.add(result, file_path, True)

        #cleanup good_cache directory
        rmtree(abspath(dir_name), ignore_errors = True)


        #TEST: populate directory with BAD file name conventions
        dir_name = 'bad_cache'
        result = OreSatFileCache(dir_name)

        file_names = ['bad_file_.txt','bad_file_1a2b3c.tar.xz','badfile12345']
        for name in file_names:
            with open(name, 'w') as fp:
                pass
            with self.assertRaises(ValueError):
                OreSatFileCache.add(result, name, True)

        with self.assertRaises(ValueError):
            OreSatFileCache(dir_name)

        #cleanup bad_cache directory
        rmtree(abspath(dir_name), ignore_errors = True)
        

    def test_oresat_file_cache_remove(self):

        #TEST: populate remove_cache with files and delete files
        dir_name = 'remove_cache'
        result = OreSatFileCache(dir_name)

        file_names = ['remove_file_12345.txt','remove_file_12345.tar.xz','remove_file_123']
        for name in file_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, True)     #True == consume the file on add
            OreSatFileCache.remove(result, name)        #remove existing file
            self.assertFalse(exists(result._dir + name))    #exists == False, file should not exist

        #try remove non-existing file
        OreSatFileCache.remove(result, 'remove_file_03152022.xyz')

        #cleanup remove_cache
        rmtree(abspath(dir_name), ignore_errors = True)


    def test_oresat_file_cache_peek(self):

        #TEST: Get the oldest file name or returns empty string if none
        dir_name = 'peek_cache'
        result = OreSatFileCache(dir_name)

        file_names = ['pop_file_12345.txt', 'pop_file_67890.txt']
        for name in file_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, True)
 
        self.assertEqual(OreSatFileCache.peek(result), file_names[0])

        #cleanup peek_cache
        rmtree(abspath(dir_name), ignore_errors = True)


    def test_oresat_file_cache_pop(self):

        #TEST: Pop an empty cache
        dir_name = 'pop_cache'
        result = OreSatFileCache(dir_name)
        phony_path = '/test/path/null'

        try:
            dest_path = OreSatFileCache.pop(result, phony_path, False)
            self.assertEqual(dest_path, '')
        except FileNotFoundError:
            print('Remove files from pop_cache or delete the directory first.')

        #TEST: Pop oldest file in cache via Move
        dir_name = 'pop_cache'
        result = OreSatFileCache(dir_name)

        dir_name2 = 'pop_copy'
        result2 = OreSatFileCache(dir_name2) #new directory for file copies

        file_names = ['pop_file_12345.txt', 'pop_file_67890.txt']
        for name in file_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, True)

        dest_path = OreSatFileCache.pop(result, abspath(dir_name2), False) #oldest file popped, Moved file into new dest
        self.assertEqual(dest_path, result2._dir + file_names[0])
        self.assertFalse(exists(result._dir + file_names[0]))   #file should not exist in original directory
        self.assertTrue(exists(result2._dir + file_names[0]))

        dest_path = OreSatFileCache.pop(result, abspath(dir_name2), True)  #oldest file popped, copy file into new destination
        self.assertEqual(dest_path, result2._dir + file_names[1])
        self.assertTrue(exists(result._dir + file_names[1]))    #file still exists in original directory
        self.assertTrue(exists(result2._dir + file_names[1]))
        
        #cleanup
        rmtree(abspath(dir_name), ignore_errors = True)
        rmtree(abspath(dir_name2), ignore_errors = True)


    def test_oresat_file_cache_get(self):

        #TEST: Get file from cache and move it to a specific directory
        dir_name = 'get_cache'
        result = OreSatFileCache(dir_name)

        dir_name2 = 'get_copy'
        result2 = OreSatFileCache(dir_name2)

        file_names = ['get_file_999.txt','get_file_111.txt']
        for name in file_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, True)
            
        dest_path = OreSatFileCache.get(result, 'get_file_111.txt', abspath(dir_name2), True)    #copy is made
        self.assertEqual(dest_path, result2._dir + 'get_file_111.txt')

        dest_path = OreSatFileCache.get(result, 'get_file_999.txt', abspath(dir_name2), False)   #file is moved
        self.assertEqual(dest_path, result2._dir + 'get_file_999.txt')

        self.assertTrue(exists(result._dir + 'get_file_111.txt'))   #File original still exist in main dir (result)
        self.assertTrue(exists(result2._dir + 'get_file_111.txt'))  #File copy is made in destination dir (result2)

        self.assertFalse(exists(result._dir + 'get_file_999.txt'))  #File original is moved from main dir (result)
        self.assertTrue(exists(result2._dir + 'get_file_999.txt'))  #File original is moved to dest dir (result2)

        #Raises: FileNotFoundError when filename is not in cache
        with self.assertRaises(FileNotFoundError):
            OreSatFileCache.get(result, 'get_file_777.txt', abspath(dir_name2), False)

        with self.assertRaises(FileNotFoundError):
            OreSatFileCache.get(result, '__get_file__', abspath(dir_name2), False)

        with self.assertRaises(FileNotFoundError):
            OreSatFileCache.get(result, 'getFile123', abspath(dir_name2), False)

        #cleanup get_cache, get_copy
        rmtree(abspath(dir_name), ignore_errors = True)
        rmtree(abspath(dir_name2), ignore_errors = True)


    def test_oresat_file_cache_files(self):
        #checlist
            #returns list of OreSatFile
            #assert file list for correctness

        #TEST: populate cache with files by keyword, then return using keyword filter
        dir_name = 'files_cache'
        result = OreSatFileCache(dir_name)

        alpha_names = ['files_alpha_123',
        'files_alpha_456.txt',
        'files_alpha_789.tar.xz']

        beta_names = ['files_beta_123',
        'files_beta_456.txt',
        'files_beta_789.tar.xz']

        gamma_names = ['files_gamma_000',
        'files_gamma_000',
        'files_gamma_000']

        for name in alpha_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, True)

        for name in beta_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, True)

        for name in gamma_names:
            with open(name, 'w') as fp:
                pass
            OreSatFileCache.add(result, name, True)

        result_files = OreSatFileCache.files(result, 'alpha') 
        for index, results in enumerate(result_files):
            self.assertEqual(results.name, alpha_names[index])

        result_files = OreSatFileCache.files(result, 'beta') 
        for index, results in enumerate(result_files):
            self.assertEqual(results.name, beta_names[index])

        result_files = OreSatFileCache.files(result, 'gamma') 
        for index, results in enumerate(result_files):
            self.assertEqual(results.name, gamma_names[index])
        self.assertEqual(len(result_files), 1)  #duplicate files are overwritten via add()

        #cleanup file_cache
        rmtree(abspath(dir_name), ignore_errors = True)


    def test_oresat_file_cache_clear(self):

        dir_name = 'clear_cache'
        result = OreSatFileCache(dir_name)
        
        file_name = 'clear_file_123.txt'
        with open(file_name, 'w') as fp:
            pass
        OreSatFileCache.clear(result)

        self.assertEqual(result.dir, abspath(dir_name))
        self.assertEqual(len(listdir(result._dir)), 0)

        #cleanup
        rmdir(dir_name)    
        remove(abspath(file_name))


if __name__ == '__main__':
    unittest.main()
