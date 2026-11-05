-- Zotero 7 本地库 zotero.sqlite 的表结构子集（建表语句取自真实库，只保留适配器会读的表）。
-- 不含任何用户数据；测试在 tmp_path 里用它建一个小库。
CREATE TABLE libraries (    libraryID INTEGER PRIMARY KEY,    type TEXT NOT NULL,    editable INT NOT NULL,    filesEditable INT NOT NULL,    version INT NOT NULL DEFAULT 0,    storageVersion INT NOT NULL DEFAULT 0,    lastSync INT NOT NULL DEFAULT 0,    archived INT NOT NULL DEFAULT 0,    isAdmin INT NOT NULL DEFAULT 0, clientVersion INT NOT NULL DEFAULT 0);
CREATE TABLE itemTypesCombined (    itemTypeID INT NOT NULL,    typeName TEXT NOT NULL,    display INT DEFAULT 1 NOT NULL,    custom INT NOT NULL,    PRIMARY KEY (itemTypeID));
CREATE TABLE fieldsCombined (    fieldID INT NOT NULL,    fieldName TEXT NOT NULL,    label TEXT,    fieldFormatID INT,    custom INT NOT NULL,    PRIMARY KEY (fieldID));
CREATE TABLE creatorTypes (    creatorTypeID INTEGER PRIMARY KEY,    creatorType TEXT);
CREATE TABLE items (    itemID INTEGER PRIMARY KEY,    itemTypeID INT NOT NULL,    dateAdded TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,    dateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,    clientDateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,    libraryID INT NOT NULL,    key TEXT NOT NULL,    version INT NOT NULL DEFAULT 0,    synced INT NOT NULL DEFAULT 0, clientVersion INT NOT NULL DEFAULT 0,    UNIQUE (libraryID, key));
CREATE TABLE itemDataValues (    valueID INTEGER PRIMARY KEY,    value UNIQUE, valueNormalized TEXT);
CREATE TABLE itemData (    itemID INT,    fieldID INT,    valueID,    PRIMARY KEY (itemID, fieldID));
CREATE TABLE creators (    creatorID INTEGER PRIMARY KEY,    firstName TEXT,    lastName TEXT,    fieldMode INT, firstNameNormalized TEXT, lastNameNormalized TEXT,    UNIQUE (lastName, firstName, fieldMode));
CREATE TABLE itemCreators (    itemID INT NOT NULL,    creatorID INT NOT NULL,    creatorTypeID INT NOT NULL DEFAULT 1,    orderIndex INT NOT NULL DEFAULT 0,    PRIMARY KEY (itemID, creatorID, creatorTypeID, orderIndex),    UNIQUE (itemID, orderIndex));
CREATE TABLE deletedItems (    itemID INTEGER PRIMARY KEY,    dateDeleted DEFAULT CURRENT_TIMESTAMP NOT NULL);
CREATE TABLE collections (    collectionID INTEGER PRIMARY KEY,    collectionName TEXT NOT NULL,    parentCollectionID INT DEFAULT NULL,    clientDateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,    libraryID INT NOT NULL,    key TEXT NOT NULL,    version INT NOT NULL DEFAULT 0,    synced INT NOT NULL DEFAULT 0, clientVersion INT NOT NULL DEFAULT 0,    UNIQUE (libraryID, key));
CREATE TABLE collectionItems (    collectionID INT NOT NULL,    itemID INT NOT NULL,    orderIndex INT NOT NULL DEFAULT 0,    PRIMARY KEY (collectionID, itemID));
CREATE TABLE itemAttachments (    itemID INTEGER PRIMARY KEY,    parentItemID INT,    linkMode INT,    contentType TEXT,    charsetID INT,    path TEXT,    syncState INT DEFAULT 0,    storageModTime INT,    storageHash TEXT,    lastProcessedModificationTime INT,    lastRead INT);

-- 取值与真实库一致的查找表
INSERT INTO libraries (libraryID, type, editable, filesEditable) VALUES (1, 'user', 1, 1), (2, 'group', 1, 1);
INSERT INTO itemTypesCombined (itemTypeID, typeName, custom) VALUES
  (1, 'annotation', 0), (3, 'attachment', 0), (7, 'book', 0), (11, 'conferencePaper', 0),
  (22, 'journalArticle', 0), (28, 'note', 0), (31, 'preprint', 0);
INSERT INTO fieldsCombined (fieldID, fieldName, custom) VALUES
  (1, 'title', 0), (2, 'abstractNote', 0), (6, 'date', 0), (8, 'DOI', 0), (10, 'url', 0),
  (19, 'extra', 0), (41, 'publicationTitle', 0), (44, 'ISSN', 0), (53, 'bookTitle', 0),
  (65, 'proceedingsTitle', 0), (69, 'repository', 0), (86, 'PMID', 0), (87, 'PMCID', 0),
  (107, 'archiveID', 0);
INSERT INTO creatorTypes (creatorTypeID, creatorType) VALUES (10, 'author'), (12, 'editor');
