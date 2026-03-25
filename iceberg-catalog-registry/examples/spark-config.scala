// Apache Spark configuration for using Apicurio Registry as an Iceberg REST Catalog
//
// Add these dependencies to your Spark session:
//   --packages org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:<version>
//
// Then configure the catalog:

spark.conf.set("spark.sql.catalog.apicurio", "org.apache.iceberg.spark.SparkCatalog")
spark.conf.set("spark.sql.catalog.apicurio.type", "rest")
spark.conf.set("spark.sql.catalog.apicurio.uri", "http://localhost:8080/apis/iceberg/v1")
spark.conf.set("spark.sql.catalog.apicurio.prefix", "default")
spark.conf.set("spark.sql.catalog.apicurio.warehouse", "s3://warehouse/data")

// S3 configuration for MinIO (from docker-compose)
spark.conf.set("spark.sql.catalog.apicurio.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
spark.conf.set("spark.sql.catalog.apicurio.s3.endpoint", "http://localhost:9000")
spark.conf.set("spark.sql.catalog.apicurio.s3.access-key-id", "minioadmin")
spark.conf.set("spark.sql.catalog.apicurio.s3.secret-access-key", "minioadmin")
spark.conf.set("spark.sql.catalog.apicurio.s3.path-style-access", "true")

// Example usage:
// spark.sql("USE apicurio")
// spark.sql("CREATE NAMESPACE analytics")
// spark.sql("CREATE TABLE apicurio.analytics.events (id BIGINT, event_type STRING, ts TIMESTAMP) USING iceberg")
// spark.sql("INSERT INTO apicurio.analytics.events VALUES (1, 'click', current_timestamp())")
// spark.sql("SELECT * FROM apicurio.analytics.events").show()
