# Using OWL for Conceptual Schema — Design Decision

> **Status: Adopted.** This document captures the original design rationale
> for emitting the conceptual schema as OWL. The recommendation has been
> implemented; the production exporter lives in
> [`schema_analyzer/owl_export.py`](../schema_analyzer/owl_export.py) and is
> exposed as `export_conceptual_model_as_owl_turtle(analysis)` and via the
> `owl` operation of the v1 tool contract.
>
> The "Implementation Strategy" section below uses **JavaScript / `rdflib.js`**
> snippets from an early Node.js prototype. Treat them as illustrative
> pseudocode for the design choices (TBox vs ABox split, `phys:` annotation
> properties, JSON-LD progressive enhancement). The shipping implementation is
> Python and uses string-builder Turtle emission rather than `rdflib`; it does
> **not** require any of the JavaScript dependencies referenced here.
>
> The decision-status block at the end of this document
> (*Recommended for approval*) is historical — OWL Turtle output has been a
> first-class artifact since 0.1.0.

## Executive Summary

**Recommendation**: ✅ **Use OWL (Web Ontology Language) for the conceptual schema** (adopted).

OWL provides significant advantages over a custom schema format and aligns perfectly with our hybrid schema use case.

---

## What is OWL?

**OWL (Web Ontology Language)** is a W3C standard for representing rich and complex knowledge about things, groups of things, and relations between things.

### Key Characteristics
- **Standard**: W3C Recommendation since 2004, OWL 2 since 2009
- **Semantic Web**: Part of the RDF/RDFS/OWL stack
- **Expressivity**: Can represent complex relationships, constraints, and hierarchies
- **Tooling**: Extensive ecosystem of tools, validators, and libraries
- **Interoperability**: Standard format understood across systems

---

## Why OWL is Perfect for Hybrid Schemas

### 1. **Separation of Concerns**
OWL naturally separates:
- **Conceptual (TBox)**: Classes, properties, relationships
- **Physical (ABox)**: Instances and their mappings

This is exactly what we need:
```turtle
# Conceptual (TBox) - Domain model
:User rdf:type owl:Class .
:Post rdf:type owl:Class .
:follows rdf:type owl:ObjectProperty ;
    rdfs:domain :User ;
    rdfs:range :User .

# Physical mapping (via custom annotations)
:User :mapsTo [
    :mappingStyle "COLLECTION" ;
    :collectionName "users"
] .

:follows :mapsTo [
    :mappingStyle "DEDICATED_COLLECTION" ;
    :edgeCollectionName "follows"
] .
```

### 2. **Expressivity for Complex Schemas**

OWL can express:

**Class Hierarchies**
```turtle
:Person rdf:type owl:Class .
:Customer rdfs:subClassOf :Person .
:PremiumCustomer rdfs:subClassOf :Customer .
```

**Property Characteristics**
```turtle
:manages rdf:type owl:ObjectProperty ;
    rdfs:domain :Employee ;
    rdfs:range :Department ;
    owl:functional true ;  # Each employee manages at most one department
    owl:inverseOf :managedBy .
```

**Cardinality Constraints**
```turtle
:User rdf:type owl:Class ;
    rdfs:subClassOf [
        rdf:type owl:Restriction ;
        owl:onProperty :hasEmail ;
        owl:cardinality 1  # Exactly one email
    ] .
```

**Disjoint Classes**
```turtle
:Human owl:disjointWith :Organization .
:Document owl:disjointWith :Person .
```

### 3. **Standard Serialization Formats**

OWL supports multiple formats:

**RDF/XML**
```xml
<owl:Class rdf:about="#User">
    <rdfs:label>User</rdfs:label>
    <rdfs:comment>A person who uses the system</rdfs:comment>
</owl:Class>
```

**Turtle (recommended for readability)**
```turtle
:User a owl:Class ;
    rdfs:label "User" ;
    rdfs:comment "A person who uses the system" .
```

**JSON-LD (excellent for APIs)**
```json
{
    "@context": "http://www.w3.org/2002/07/owl#",
    "@id": "User",
    "@type": "Class",
    "rdfs:label": "User",
    "rdfs:comment": "A person who uses the system"
}
```

### 4. **Validation and Reasoning**

OWL enables:

**Consistency Checking**
- Detect contradictions in schema
- Validate property usage
- Check cardinality constraints

**Type Inference**
```turtle
# If we define:
:Employee rdfs:subClassOf :Person .
:john rdf:type :Employee .

# Reasoner can infer:
:john rdf:type :Person .
```

**Property Inference**
```turtle
# If we define:
:manages owl:inverseOf :managedBy .
:alice :manages :engineering .

# Reasoner can infer:
:engineering :managedBy :alice .
```

### 5. **Rich Ecosystem**

**Libraries (JavaScript/Node.js)**
- `rdflib.js` - RDF library for JavaScript
- `jsonld` - JSON-LD processor
- `n3` - Fast RDF parser and writer
- `sparql-engine` - SPARQL query engine

**Tools**
- Protégé - Visual ontology editor
- OWL API - Java library for OWL manipulation
- Apache Jena - Semantic web framework
- RDFLib (Python) - RDF manipulation

**Validators**
- HermiT - OWL 2 reasoner
- Pellet - OWL DL reasoner
- OWL Validator - Web-based validation

### 6. **Interoperability**

OWL ontologies can:
- Be shared across systems
- Be published as Linked Data
- Integrate with external ontologies
- Support SPARQL queries

This means our conceptual schema could:
```sparql
# Query the conceptual schema itself using SPARQL
PREFIX : <http://arangodb.com/schema/hybrid#>

SELECT ?class ?property
WHERE {
    ?class a owl:Class .
    ?property rdfs:domain ?class .
}
```

---

## Comparison: OWL vs Custom Format

| Aspect | OWL | Custom Format |
|--------|-----|---------------|
| **Standardization** | W3C Standard | Custom (requires documentation) |
| **Tooling** | Extensive ecosystem | Must build everything |
| **Validation** | Built-in validators | Must implement |
| **Reasoning** | Automatic inference | Must implement |
| **Serialization** | RDF/XML, Turtle, JSON-LD | Custom (must define) |
| **Learning Curve** | Moderate (existing knowledge) | Low (we design it) |
| **Expressivity** | Very high | As high as we implement |
| **Interoperability** | High (standard format) | Low (custom format) |
| **Future-proof** | Yes (maintained standard) | Depends on us |

**Verdict**: OWL wins on almost every dimension except initial learning curve.

---

## Recommended OWL Profile: OWL 2 DL

**OWL 2 DL** (Description Logic) provides:
- Rich expressivity
- Decidable reasoning (guaranteed termination)
- Widely supported by tools
- Good balance of power and complexity

Alternative profiles:
- **OWL 2 EL**: Simpler, very fast reasoning (if we need performance)
- **OWL 2 QL**: Query-optimized (if we need query rewriting)
- **OWL 2 RL**: Rule-based reasoning (if we need forward chaining)

**Recommendation**: Start with OWL 2 DL, profile down if needed.

---

## Architecture with OWL

### Conceptual Schema as OWL Ontology

```turtle
@prefix : <http://arangodb.com/schema/hybrid#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

# Ontology metadata
: a owl:Ontology ;
    rdfs:label "Hybrid Graph Schema Ontology" ;
    rdfs:comment "Conceptual schema for hybrid ArangoDB graph" ;
    owl:versionInfo "1.0" .

# Entity Classes
:User a owl:Class ;
    rdfs:label "User" ;
    rdfs:comment "A person who uses the system" .

:Post a owl:Class ;
    rdfs:label "Post" ;
    rdfs:comment "A blog post or article" .

:Comment a owl:Class ;
    rdfs:label "Comment" ;
    rdfs:comment "A comment on a post" .

# Relationship Properties
:follows a owl:ObjectProperty ;
    rdfs:label "follows" ;
    rdfs:domain :User ;
    rdfs:range :User ;
    rdfs:comment "User follows another user" .

:authored a owl:ObjectProperty ;
    rdfs:label "authored" ;
    rdfs:domain :User ;
    rdfs:range :Post ;
    rdfs:comment "User authored a post" ;
    owl:inverseOf :authoredBy .

:commentedOn a owl:ObjectProperty ;
    rdfs:label "commented on" ;
    rdfs:domain :Comment ;
    rdfs:range :Post .

# Data Properties
:hasEmail a owl:DatatypeProperty ;
    rdfs:domain :User ;
    rdfs:range xsd:string ;
    owl:cardinality 1 .

:hasName a owl:DatatypeProperty ;
    rdfs:domain :User ;
    rdfs:range xsd:string ;
    owl:minCardinality 1 .
```

### Physical Mapping as RDF Annotations

We can extend OWL with custom annotations for physical mappings. The export declares
every annotation property it can write in the Turtle header, whether or not a given
analysis uses it:

```turtle
@prefix phys: <http://arangodb.com/schema/physical#> .

# The annotation properties the export declares
phys:mappingStyle a owl:AnnotationProperty .
phys:collectionName a owl:AnnotationProperty .
phys:typeField a owl:AnnotationProperty .
phys:typeValue a owl:AnnotationProperty .
phys:labelsField a owl:AnnotationProperty .
phys:labelValue a owl:AnnotationProperty .
phys:edgeCollectionName a owl:AnnotationProperty .
phys:observedCardinality a owl:AnnotationProperty .
phys:attributeName a owl:AnnotationProperty .
phys:attributePath a owl:AnnotationProperty .
phys:multiValued a owl:AnnotationProperty .

# Physical mappings on classes
:User phys:mappingStyle "COLLECTION" ;
    phys:collectionName "users" .

:Post phys:mappingStyle "LABEL" ;
    phys:collectionName "entities" ;
    phys:typeField "type" ;
    phys:typeValue "post" .

:Person phys:mappingStyle "LABELS" ;
    phys:collectionName "nodes" ;
    phys:labelsField "labels" ;
    phys:labelValue "Person" .

# Physical mappings on object properties
:follows phys:mappingStyle "DEDICATED_COLLECTION" ;
    phys:edgeCollectionName "follows" ;
    phys:observedCardinality "N:M" .

:authored phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:edgeCollectionName "relationships" ;
    phys:typeField "relation" ;
    phys:typeValue "authored" .

# Physical locations on datatype properties
:email phys:attributeName "email_address" .
:city phys:attributePath "[\"address\", \"city\"]" .
:tags phys:multiValued "true" .
```

| Annotation | On | Value |
|---|---|---|
| `phys:mappingStyle` | class, object property | The mapping style: `COLLECTION`, `LABEL`, `LABELS`, `DEDICATED_COLLECTION`, `GENERIC_WITH_TYPE`, … |
| `phys:collectionName` | class | The document collection holding the class's documents. |
| `phys:typeField` | class, object property | The discriminator field of a `LABEL` class or a `GENERIC_WITH_TYPE` edge. |
| `phys:typeValue` | class, object property | The discriminator value that selects this class or relationship. |
| `phys:labelsField` | class | For `LABELS`: the field holding each document's list of labels. |
| `phys:labelValue` | class | For `LABELS`: the label in that list that selects this class. |
| `phys:edgeCollectionName` | object property | The edge collection holding the relationship's edges. |
| `phys:observedCardinality` | object property | `1:1`, `1:N`, `N:1` or `N:M`, from sampled statistics (`metadata.statistics`); the export also marks `owl:FunctionalProperty` / `owl:InverseFunctionalProperty` from it. It is what was observed, not a constraint. |
| `phys:attributeName` | datatype property | The stored field, written only when it differs from the property's IRI local name. |
| `phys:attributePath` | datatype property | For a value inside a nested object: a JSON array with one name per level. `phys:attributeName` is then absent. |
| `phys:multiValued` | datatype property | `"true"` when the field holds a list: read one value per element. |

The JSON-LD export carries the same annotations on the same nodes.

---

## Implementation Strategy

### Phase 1: Core OWL Support

**Week 1-2**: Basic OWL infrastructure
```javascript
// packages/schema-analyzer/src/owl-schema.js

const rdflib = require('rdflib');
const { Store, Namespace, Literal } = rdflib;

class OWLConceptualSchema {
  constructor() {
    this.store = new Store();
    this.OWL = Namespace('http://www.w3.org/2002/07/owl#');
    this.RDF = Namespace('http://www.w3.org/1999/02/22-rdf-syntax-ns#');
    this.RDFS = Namespace('http://www.w3.org/2000/01/rdf-schema#');
    this.SCHEMA = Namespace('http://arangodb.com/schema/hybrid#');
  }
  
  addClass(classIRI, label, comment) {
    const classNode = this.SCHEMA(classIRI);
    this.store.add(classNode, this.RDF('type'), this.OWL('Class'));
    this.store.add(classNode, this.RDFS('label'), new Literal(label));
    if (comment) {
      this.store.add(classNode, this.RDFS('comment'), new Literal(comment));
    }
  }
  
  addObjectProperty(propertyIRI, domain, range, label) {
    const propNode = this.SCHEMA(propertyIRI);
    this.store.add(propNode, this.RDF('type'), this.OWL('ObjectProperty'));
    this.store.add(propNode, this.RDFS('domain'), this.SCHEMA(domain));
    this.store.add(propNode, this.RDFS('range'), this.SCHEMA(range));
    this.store.add(propNode, this.RDFS('label'), new Literal(label));
  }
  
  exportTurtle() {
    return new Promise((resolve, reject) => {
      this.store.serialize(null, 'text/turtle', (err, turtle) => {
        if (err) reject(err);
        else resolve(turtle);
      });
    });
  }
  
  exportJSONLD() {
    return new Promise((resolve, reject) => {
      this.store.serialize(null, 'application/ld+json', (err, jsonld) => {
        if (err) reject(err);
        else resolve(JSON.parse(jsonld));
      });
    });
  }
  
  async importTurtle(turtleString) {
    return new Promise((resolve, reject) => {
      this.store.parse(turtleString, this.store, 'text/turtle', (err) => {
        if (err) reject(err);
        else resolve();
      });
    });
  }
}
```

### Phase 2: Physical Mapping Extension

**Week 3**: Custom annotations for physical mappings
```javascript
// packages/schema-analyzer/src/physical-mapping-owl.js

class PhysicalMappingOWL {
  constructor(owlSchema) {
    this.schema = owlSchema;
    this.PHYS = Namespace('http://arangodb.com/schema/physical#');
  }
  
  addEntityMapping(classIRI, mappingConfig) {
    const classNode = this.schema.SCHEMA(classIRI);
    
    this.schema.store.add(
      classNode,
      this.PHYS('mappingStyle'),
      new Literal(mappingConfig.style)
    );
    
    if (mappingConfig.collectionName) {
      this.schema.store.add(
        classNode,
        this.PHYS('collectionName'),
        new Literal(mappingConfig.collectionName)
      );
    }
    
    if (mappingConfig.typeField) {
      this.schema.store.add(
        classNode,
        this.PHYS('typeField'),
        new Literal(mappingConfig.typeField)
      );
      this.schema.store.add(
        classNode,
        this.PHYS('typeValue'),
        new Literal(mappingConfig.typeValue)
      );
    }
  }
  
  getEntityMapping(classIRI) {
    const classNode = this.schema.SCHEMA(classIRI);
    
    const style = this.schema.store.any(
      classNode,
      this.PHYS('mappingStyle'),
      null
    );
    
    if (!style) return null;
    
    return {
      style: style.value,
      collectionName: this.schema.store.any(
        classNode,
        this.PHYS('collectionName'),
        null
      )?.value,
      typeField: this.schema.store.any(
        classNode,
        this.PHYS('typeField'),
        null
      )?.value,
      typeValue: this.schema.store.any(
        classNode,
        this.PHYS('typeValue'),
        null
      )?.value
    };
  }
}
```

### Phase 3: Query Translation with OWL

**Week 4**: Translate Cypher using OWL schema
```javascript
// src/lib/owl-cypher-translator.js

class OWLCypherTranslator {
  constructor(owlSchema, physicalMapping) {
    this.schema = owlSchema;
    this.mapping = physicalMapping;
  }
  
  translatePattern(pattern) {
    const nodeLabel = pattern.startNode.labels[0];
    const classIRI = this.resolveClassIRI(nodeLabel);
    
    // Get physical mapping from OWL annotations
    const mapping = this.mapping.getEntityMapping(classIRI);
    
    if (mapping.style === 'COLLECTION') {
      return `FOR ${pattern.startNode.variable} IN ${mapping.collectionName}`;
    } else if (mapping.style === 'LABEL') {
      return `FOR ${pattern.startNode.variable} IN ${mapping.collectionName}
              FILTER ${pattern.startNode.variable}.${mapping.typeField} == "${mapping.typeValue}"`;
    }
  }
  
  resolveClassIRI(cypherLabel) {
    // Map Cypher label to OWL class IRI
    // Could use rdfs:label to match
    const matches = this.schema.store.match(
      null,
      this.schema.RDFS('label'),
      new Literal(cypherLabel)
    );
    
    if (matches.length > 0) {
      return matches[0].subject.value;
    }
    
    throw new Error(`No OWL class found for Cypher label: ${cypherLabel}`);
  }
}
```

---

## Benefits of OWL Approach

### 1. **Standard Validation**
```javascript
// Validate OWL ontology using standard tools
const { validateOWL } = require('owl-validator');

const validation = await validateOWL(schema.exportTurtle());
if (!validation.valid) {
  console.error('Schema errors:', validation.errors);
}
```

### 2. **Automatic Reasoning**
```javascript
// Use reasoner to infer implicit relationships
const { HermiTReasoner } = require('hermit-reasoner');

const reasoner = new HermiTReasoner(schema);
const inferredTriples = reasoner.materialize();

// E.g., if PremiumCustomer subClassOf Customer
// and john is a PremiumCustomer
// reasoner infers john is also a Customer
```

### 3. **SPARQL Queries on Schema**
```javascript
// Query the conceptual schema itself
const query = `
  PREFIX owl: <http://www.w3.org/2002/07/owl#>
  PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
  PREFIX : <http://arangodb.com/schema/hybrid#>
  
  SELECT ?class ?label
  WHERE {
    ?class a owl:Class .
    ?class rdfs:label ?label .
  }
`;

const results = await schema.query(query);
// Returns all classes in the ontology
```

### 4. **Schema Evolution**
```javascript
// Load existing schema
const existingSchema = await OWLConceptualSchema.load('schema-v1.ttl');

// Add new class
existingSchema.addClass('PremiumUser', 'Premium User', 'A premium subscriber');
existingSchema.addSubClassRelation('PremiumUser', 'User');

// Save new version
await existingSchema.save('schema-v2.ttl');
```

### 5. **Documentation Generation**
```javascript
// Auto-generate schema documentation
const { generateDocs } = require('owl-doc-generator');

const docs = generateDocs(schema, {
  format: 'markdown',
  includeHierarchy: true,
  includeProperties: true,
  includeExamples: true
});

// Produces comprehensive schema documentation
```

---

## Challenges and Mitigations

### Challenge 1: Learning Curve
**Impact**: Team needs to learn OWL/RDF concepts

**Mitigation**:
- Use high-level wrapper classes (hide complexity)
- Provide examples and templates
- Start with simple ontologies
- Use Turtle format (most readable)

### Challenge 2: Library Dependencies
**Impact**: Additional npm dependencies

**Mitigation**:
- Use well-maintained libraries (rdflib.js, n3)
- Keep dependencies minimal
- Provide fallback to custom format if needed

### Challenge 3: Performance
**Impact**: RDF parsing/serialization overhead

**Mitigation**:
- Cache parsed ontologies
- Use efficient RDF libraries (n3 is very fast)
- Profile and optimize hot paths
- Consider OWL 2 EL profile for simpler reasoning

### Challenge 4: Overkill for Simple Cases
**Impact**: OWL might be too powerful for simple schemas

**Mitigation**:
- Use OWL selectively (only for complex hybrid schemas)
- Provide simplified API that hides OWL complexity
- Generate OWL from simple JSON for basic cases

---

## Recommendation

### Primary Recommendation: **Use OWL with Progressive Enhancement**

**Phase 1** (Weeks 1-4): Use simple JSON-LD subset
```javascript
// Simple, OWL-compatible but easy to use
{
  "@context": "http://www.w3.org/2002/07/owl#",
  "classes": {
    "User": {
      "@type": "Class",
      "label": "User"
    }
  },
  "properties": {
    "follows": {
      "@type": "ObjectProperty",
      "domain": "User",
      "range": "User"
    }
  }
}
```

**Phase 2** (Weeks 5-8): Add OWL expressivity as needed
```turtle
# Graduate to full OWL when we need hierarchy, constraints, etc.
:PremiumUser rdfs:subClassOf :User .
:User rdfs:subClassOf [
    a owl:Restriction ;
    owl:onProperty :hasEmail ;
    owl:cardinality 1
] .
```

**Phase 3** (Weeks 9-12): Full OWL integration
- Reasoning
- Validation
- SPARQL queries
- Schema evolution

### Benefits of This Approach
✅ Start simple (JSON-LD is just JSON)
✅ Standard-compliant from day one
✅ Can upgrade to full OWL gradually
✅ Tooling compatibility throughout
✅ No technical debt (all formats interoperable)

---

## Conclusion

**Use OWL for the conceptual schema because:**

1. ✅ It's a **standard** - No need to invent our own format
2. ✅ **Rich expressivity** - Handles complex schemas naturally
3. ✅ **Extensive tooling** - Validators, reasoners, editors
4. ✅ **Multiple serializations** - JSON-LD, Turtle, RDF/XML
5. ✅ **Future-proof** - Maintained W3C standard
6. ✅ **Interoperable** - Can integrate with other systems
7. ✅ **Validates** - Built-in consistency checking
8. ✅ **Reasons** - Automatic inference of implicit facts
9. ✅ **Documents** - Self-documenting schema
10. ✅ **Evolves** - Schema versioning built-in

**The learning curve is worth it** for the long-term benefits.

---

## Further Reading

- **OWL 2 Primer**: https://www.w3.org/TR/owl2-primer/
- **OWL 2 Quick Reference**: https://www.w3.org/TR/owl2-quick-reference/
- **RDFLib.js Documentation**: https://github.com/linkeddata/rdflib.js
- **JSON-LD Playground**: https://json-ld.org/playground/
- **Protégé Tutorial**: https://protegewiki.stanford.edu/wiki/Pr5_UG_Getting_Started

---

**Decision Status**: Recommended for approval  
**Impact**: Medium (additional libraries, learning curve)  
**Benefit**: High (standard, tooling, future-proof)  
**Risk**: Low (well-established technology)
