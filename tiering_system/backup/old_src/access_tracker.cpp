/*
 * CephFS Tiering System - Access Tracker
 * Consumes eBPF events and updates RocksDB + PostgreSQL
 */

#include <iostream>
#include <string>
#include <thread>
#include <atomic>
#include <chrono>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>
#include <rocksdb/db.h>
#include <pqxx/pqxx>
#include <sys/stat.h>
#include <unistd.h>

#define PATH_MAX 256

struct access_event {
    uint64_t inode;
    uint64_t timestamp_ns;
    uint32_t pid;
    uint32_t uid;
    char path[PATH_MAX];
};

class AccessTracker {
private:
    struct bpf_object *bpf_obj;
    struct ring_buffer *rb;
    rocksdb::DB *rocks_db;
    std::shared_ptr<pqxx::connection> pg_conn;
    std::atomic<bool> running;
    std::atomic<uint64_t> events_processed;
    std::thread pg_flusher_thread;
    
    static int handle_event(void *ctx, void *data, size_t size) {
        AccessTracker *tracker = static_cast<AccessTracker*>(ctx);
        return tracker->process_event(data, size);
    }
    
    int process_event(void *data, size_t size) {
        if (size != sizeof(access_event))
            return 0;
        
        auto *event = static_cast<access_event*>(data);
        
        // Update RocksDB (hot path - fast writes)
        std::string key = std::to_string(event->inode);
        std::string value = std::to_string(event->timestamp_ns) + "|" + 
                           std::string(event->path);
        
        rocksdb::Status s = rocks_db->Put(rocksdb::WriteOptions(), key, value);
        if (!s.ok()) {
            std::cerr << "RocksDB write failed: " << s.ToString() << std::endl;
            return -1;
        }
        
        events_processed++;
        
        if (events_processed % 10000 == 0) {
            std::cout << "Processed " << events_processed << " events" << std::endl;
        }
        
        return 0;
    }
    
    void postgres_flusher() {
        while (running) {
            std::this_thread::sleep_for(std::chrono::seconds(60));
            
            try {
                flush_to_postgres();
            } catch (const std::exception &e) {
                std::cerr << "PostgreSQL flush error: " << e.what() << std::endl;
            }
        }
    }
    
    void flush_to_postgres() {
        std::cout << "Flushing to PostgreSQL..." << std::endl;
        
        // Iterate RocksDB and batch update PostgreSQL
        rocksdb::Iterator* it = rocks_db->NewIterator(rocksdb::ReadOptions());
        
        pqxx::work txn(*pg_conn);
        int batch_count = 0;
        
        for (it->SeekToFirst(); it->Valid(); it->Next()) {
            std::string inode_str = it->key().ToString();
            std::string value = it->value().ToString();
            
            // Parse value: "timestamp_ns|path"
            size_t delimiter = value.find('|');
            if (delimiter == std::string::npos)
                continue;
            
            uint64_t timestamp_ns = std::stoull(value.substr(0, delimiter));
            std::string path = value.substr(delimiter + 1);
            
            // Convert ns to timestamp
            auto timestamp = std::chrono::system_clock::from_time_t(
                timestamp_ns / 1000000000ULL
            );
            
            // Upsert into PostgreSQL
            txn.exec_params(
                "INSERT INTO file_metadata (inode, path, last_access, access_count) "
                "VALUES ($1, $2, to_timestamp($3), 1) "
                "ON CONFLICT (inode) DO UPDATE SET "
                "  path = EXCLUDED.path, "
                "  last_access = EXCLUDED.last_access, "
                "  access_count = file_metadata.access_count + 1, "
                "  updated_at = NOW()",
                inode_str,
                path,
                static_cast<double>(timestamp_ns) / 1e9
            );
            
            batch_count++;
            
            if (batch_count % 1000 == 0) {
                txn.commit();
                txn = pqxx::work(*pg_conn);
            }
        }
        
        if (batch_count % 1000 != 0) {
            txn.commit();
        }
        
        delete it;
        
        std::cout << "Flushed " << batch_count << " records to PostgreSQL" << std::endl;
    }
    
public:
    AccessTracker(const std::string &bpf_path, 
                 const std::string &rocks_path,
                 const std::string &pg_connstr) 
        : bpf_obj(nullptr), rb(nullptr), rocks_db(nullptr), 
          running(false), events_processed(0) {
        
        // Open RocksDB
        rocksdb::Options options;
        options.create_if_missing = true;
        options.write_buffer_size = 64 * 1024 * 1024;  // 64MB
        options.max_write_buffer_number = 3;
        options.target_file_size_base = 64 * 1024 * 1024;
        
        rocksdb::Status s = rocksdb::DB::Open(options, rocks_path, &rocks_db);
        if (!s.ok()) {
            throw std::runtime_error("Failed to open RocksDB: " + s.ToString());
        }
        
        std::cout << "RocksDB opened at " << rocks_path << std::endl;
        
        // Connect to PostgreSQL
        pg_conn = std::make_shared<pqxx::connection>(pg_connstr);
        if (!pg_conn->is_open()) {
            throw std::runtime_error("Failed to connect to PostgreSQL");
        }
        
        std::cout << "PostgreSQL connected" << std::endl;
        
        // Load eBPF program
        bpf_obj = bpf_object__open_file(bpf_path.c_str(), nullptr);
        if (libbpf_get_error(bpf_obj)) {
            throw std::runtime_error("Failed to open BPF object");
        }
        
        if (bpf_object__load(bpf_obj)) {
            throw std::runtime_error("Failed to load BPF object");
        }
        
        // Attach programs
        bpf_object__for_each_program(prog, bpf_obj) {
            if (bpf_program__attach(prog) < 0) {
                throw std::runtime_error("Failed to attach BPF program");
            }
        }
        
        std::cout << "eBPF programs attached" << std::endl;
        
        // Setup ring buffer
        struct bpf_map *map = bpf_object__find_map_by_name(bpf_obj, "events");
        if (!map) {
            throw std::runtime_error("Failed to find events map");
        }
        
        int map_fd = bpf_map__fd(map);
        rb = ring_buffer__new(map_fd, handle_event, this, nullptr);
        if (!rb) {
            throw std::runtime_error("Failed to create ring buffer");
        }
        
        std::cout << "Ring buffer initialized" << std::endl;
    }
    
    ~AccessTracker() {
        stop();
        
        if (rb) ring_buffer__free(rb);
        if (bpf_obj) bpf_object__close(bpf_obj);
        if (rocks_db) delete rocks_db;
    }
    
    void start() {
        running = true;
        
        // Start PostgreSQL flusher thread
        pg_flusher_thread = std::thread(&AccessTracker::postgres_flusher, this);
        
        std::cout << "Access tracker started" << std::endl;
        
        // Main event loop
        while (running) {
            int ret = ring_buffer__poll(rb, 100);  // 100ms timeout
            if (ret < 0 && ret != -EINTR) {
                std::cerr << "Ring buffer poll error: " << ret << std::endl;
                break;
            }
        }
    }
    
    void stop() {
        if (running) {
            running = false;
            
            if (pg_flusher_thread.joinable()) {
                pg_flusher_thread.join();
            }
            
            // Final flush
            flush_to_postgres();
            
            std::cout << "Access tracker stopped. Total events: " 
                     << events_processed << std::endl;
        }
    }
};

int main(int argc, char **argv) {
    if (argc != 4) {
        std::cerr << "Usage: " << argv[0] 
                 << " <bpf_object.o> <rocksdb_path> <pg_connstr>" << std::endl;
        std::cerr << "Example: " << argv[0] 
                 << " cephfs_tracker.bpf.o /var/lib/tiering/rocks "
                 << "'host=localhost dbname=tiering user=postgres'" << std::endl;
        return 1;
    }
    
    try {
        AccessTracker tracker(argv[1], argv[2], argv[3]);
        
        // Handle signals for graceful shutdown
        signal(SIGINT, [](int) {
            std::cout << "\nShutting down..." << std::endl;
        });
        
        tracker.start();
        
    } catch (const std::exception &e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }
    
    return 0;
}
